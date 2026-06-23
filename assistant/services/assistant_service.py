import os
import time
import asyncio
import re
import math
from pathlib import Path
from typing import Dict, Any, List, Tuple
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig
from sentence_transformers import SentenceTransformer
from repositories.assistant_repo import AssistantRepository

# Device configuration
def _get_device_dtype():
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

    if torch.backends.mps.is_available():
        return torch.device("mps"), torch.float16
    elif torch.cuda.is_available():
        return torch.device("cuda"), torch.float16
    else:
        return torch.device("cpu"), torch.float32

DEVICE, DTYPE = _get_device_dtype()

if DEVICE.type == "cuda":
    try:
        _gpu_name = torch.cuda.get_device_name(0)
        _gpu_count = torch.cuda.device_count()
    except Exception:
        _gpu_name, _gpu_count = "unknown", "?"
    print(
        f"[assistant_service] inference device: CUDA ({_gpu_name}, "
        f"{_gpu_count} device(s)), dtype={DTYPE}",
        flush=True,
    )
elif DEVICE.type == "mps":
    print(
        f"[assistant_service] inference device: Apple MPS, dtype={DTYPE}",
        flush=True,
    )
else:
    print(
        f"[assistant_service] inference device: CPU (no CUDA / MPS), "
        f"dtype={DTYPE} - LLM will be slow",
        flush=True,
    )

try:
    torch.set_num_threads(max(1, os.cpu_count() or 4))
except Exception:
    pass

_EMBEDDER = None
_TOKENIZER = None
_LLM = None
_GEN_CONFIG = None

_EMBED_CACHE: Dict[str, List[float]] = {}
_EMBED_CACHE_MAX = 64

def _embed_cache_get(key: str):
    return _EMBED_CACHE.get(key)

def _embed_cache_put(key: str, value: List[float]):
    if key in _EMBED_CACHE:
        _EMBED_CACHE[key] = value
        return
    if len(_EMBED_CACHE) >= _EMBED_CACHE_MAX:
        _EMBED_CACHE.pop(next(iter(_EMBED_CACHE)))
    _EMBED_CACHE[key] = value

def get_embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        hf_token = os.getenv("HF_TOKEN")
        _EMBEDDER = SentenceTransformer(
            "all-MiniLM-L6-v2",
            token=hf_token,
            device="mps" if DEVICE.type == "mps" else DEVICE.type
        )
    return _EMBEDDER

def get_llm():
    global _TOKENIZER, _LLM, _GEN_CONFIG
    # Temporary selection of llm model
    if _LLM is None:
        mode = (os.getenv("ASSISTANT_MODE") or "offline").strip().lower()
        if mode == "online":
            model_id = os.getenv("ASSISTANT_ONLINE_MODEL_ID") or "" # To Determine which model to use
        else:
            model_id = os.getenv("ASSISTANT_OFFLINE_MODEL_ID") or "HuggingFaceTB/SmolLM2-360M-Instruct"
        hf_token = os.getenv("HF_TOKEN")

        _TOKENIZER = AutoTokenizer.from_pretrained(
            model_id, token=hf_token, use_fast=True
        )

        _LLM = AutoModelForCausalLM.from_pretrained(
            model_id,
            token=hf_token,
            dtype=DTYPE,
            low_cpu_mem_usage=True
        ).to(DEVICE)
        _LLM.eval()
        torch.set_grad_enabled(False)

        # do_sample = off to prevent hallucinations
        _GEN_CONFIG = GenerationConfig(
            max_new_tokens=150,
            do_sample=False,
            use_cache=True,
            pad_token_id=_TOKENIZER.eos_token_id,
            eos_token_id=_TOKENIZER.eos_token_id,
            repetition_penalty=1.2,
        )
    return _TOKENIZER, _LLM, _GEN_CONFIG

def _count_tokens(tokenizer: AutoTokenizer, text: str) -> int:
    return len(tokenizer(text, add_special_tokens=False)["input_ids"])

def _truncate_context(tokenizer: AutoTokenizer, prefix: str, question: str, max_input_tokens: int = 1200) -> Tuple[str, str]:
    q_tokens = _count_tokens(tokenizer, question)
    budget_for_context = max(0, max_input_tokens - q_tokens)
    if _count_tokens(tokenizer, prefix) <= budget_for_context:
        return prefix, question

    lines = prefix.splitlines()
    kept = []
    for line in lines:
        kept.append(line)
        if _count_tokens(tokenizer, "\n".join(kept)) > budget_for_context:
            kept.pop()
            break
    return "\n".join(kept), question

_FIND_PLACE_HIGH_SCORE = 0.55
_FIND_PLACE_LOW_SCORE  = 0.15

_FIND_PLACE_STOPWORDS = {
    "where", "is", "are", "the", "a", "an", "in", "on", "of", "to", "and",
    "or", "do", "does", "did", "have", "has", "had", "can", "could", "would",
    "should", "shall", "will", "may", "might", "i", "me", "my", "you", "your",
    "we", "us", "they", "them", "it", "its", "this", "that", "these", "those",
    "here", "there", "any", "some", "find", "locate", "located", "look",
    "looking", "for", "what", "which", "who", "whom", "how", "please",
    "where's", "tell", "show",
    # Domain-specific filler:
    "room", "rooms", "space", "spaces", "building", "buildings", "floor",
    "floors", "place", "places", "area", "areas",
}

_FIND_PLACE_SYNONYMS = {
    "toilet":     ("bathroom", "restroom", "wc", "toilet"),
    "bathroom":   ("bathroom", "restroom", "wc", "toilet"),
    "restroom":   ("bathroom", "restroom", "wc", "toilet"),
    "wc":         ("bathroom", "restroom", "wc", "toilet"),
    "lift":       ("elevator", "lift"),
    "elevator":   ("elevator", "lift"),
    "stairs":     ("stair", "staircase", "stairwell"),
    "stair":      ("stair", "staircase", "stairwell"),
    "staircase":  ("stair", "staircase", "stairwell"),
    "hallway":    ("hallway", "corridor"),
    "hall":       ("hallway", "corridor"),
    "corridor":   ("hallway", "corridor"),
    "cafe":       ("cafe", "cafeteria", "canteen"),
    "cafeteria":  ("cafe", "cafeteria", "canteen"),
    "canteen":    ("cafe", "cafeteria", "canteen"),
    "entrance":   ("entrance", "entry", "lobby"),
    "entry":      ("entrance", "entry", "lobby"),
    "lobby":      ("entrance", "entry", "lobby"),
    "exit":       ("exit", "entrance"),
    "auditorium": ("auditorium", "aula", "lecture"),
}


def _depluralise(token: str) -> str:
    """Strip a common English plural suffix so 'wcs' → 'wc', 'toilets' →
    'toilet', 'rooms' → 'room'. Conservative: only trims when the rest of
    the token is still at least 2 chars, so we don't eat 'is' down to 'i'."""
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("es"):
        return token[:-2]
    if len(token) > 2 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _find_place_token_match(query: str, candidate_name: str, candidate_type: str | None) -> bool:
    """True iff at least one non-stopword query token (or its singular form,
    or a sanctioned synonym) appears in the candidate space's display name
    or its space_type. Catches the "where is room 999?" failure mode where
    the cosine threshold passes but the answer is unrelated to what was asked.
    """
    haystack = f"{candidate_name or ''} {candidate_type or ''}".lower()
    if not haystack.strip():
        return False
    tokens = re.findall(r"[a-z0-9][a-z0-9'_-]*", (query or "").lower())
    content = [t for t in tokens if t not in _FIND_PLACE_STOPWORDS and len(t) >= 2]
    if not content:
        # Query was all stopwords — fall back to the cosine score alone.
        return True
    for raw in content:
        for tok in {raw, _depluralise(raw)}:
            if tok in haystack:
                return True
            for needle in _FIND_PLACE_SYNONYMS.get(tok, ()):
                if needle in haystack:
                    return True
    return False


def _floor_label(floor_name, floor_index) -> str:
    """Human floor description. Prefers the floor's display name, else derives
    one from the index (so a null name no longer renders as "located on None")."""
    if floor_name:
        return str(floor_name)
    if floor_index is None:
        return "an unspecified floor"
    if floor_index == 0:
        return "the ground floor (floor 0)"
    if floor_index < 0:
        return f"basement level {abs(int(floor_index))}"
    return f"floor {int(floor_index)}"

def clean_response(text: str) -> str:
    """Prevents the LLM to return unnecessary reasoning in the output"""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    if re.search(r"(?i)\bso,\s*(you would need|to get to|just|walk|go)", text):
        parts = re.split(r"(?i)\bso,\s*", text)
        if len(parts) > 1:
            text = parts[-1]
            text = text[0].upper() + text[1:]

    patterns = [
        r"(?i)^.*?this can be determined.*?:",
        r"(?i)^.*?by examining.*?:",
        r"(?i)^.*?let(?:'s)? think.*?:",
        r"(?i)^.*?okay.*?:",
        r"(?i)^.*?looking at the.*?:",
        r"(?i)^.*?here(?:'s)? how.*?:",
        r"(?i)^.*?we can see that.*?:"
    ]
    for p in patterns:
        text = re.sub(p, "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*(?:\d+\.|[-*])\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n+", " ", text, flags=re.MULTILINE)

    text = text.strip()
    if re.search(r"(?i)^(?:so\b|therefore\b|thus\b|based on.*?\b|as a result\b)", text):
        text = re.sub(r"(?i)^(?:so\b|therefore\b|thus\b|based on.*?\b|as a result\b)[^.!?]*[.!?]?\s*", "", text)
    if len(text) < 5 or re.fullmatch(r"\d+\.?", text):
        return text
    
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) > 1:
        text = " ".join(sentences[:2])

    return text.strip()


def _generate_sync(messages: List[Dict[str, str]]) -> str:
    tokenizer, llm, gen_config = get_llm()

    text = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )

    inputs = tokenizer(text, return_tensors="pt").to(DEVICE)

    with torch.inference_mode():
        output_ids = llm.generate(**inputs, generation_config=gen_config)
        gen_only = output_ids[:, inputs.input_ids.shape[-1]:]
        response = tokenizer.batch_decode(gen_only, skip_special_tokens=True)[0]

    return clean_response(response)

class AssistantService:
    def __init__(self, db, pg_db=None):
        self.repo = AssistantRepository(db, pg_db=pg_db)

    async def _encode_query(self, text: str) -> List[float]:
        cached = _embed_cache_get(text)
        if cached is not None:
            return cached

        embedder = get_embedder()
        vec = await asyncio.to_thread(
            lambda: embedder.encode(text, convert_to_numpy=True).tolist()
        )
        _embed_cache_put(text, vec)
        return vec

    def _smalltalk_reply(self, user_query: str) -> str | None:
        """Return a canned reply for greetings / thanks / trivial chit-chat, else None."""
        q = user_query.lower().strip().strip("!.?,")
        if not q:
            return None
        greetings = {
            "hi", "hey", "hello", "yo", "hiya", "howdy", "heya",
            "hey there", "hello there", "hi there", "good morning",
            "good afternoon", "good evening", "greetings", "sup",
            "what's up", "whats up", "wassup",
        }
        thanks = {
            "thanks", "thank you", "thx", "ty", "cheers",
            "thank you so much", "thanks a lot", "appreciate it",
        }
        farewells = {"bye", "goodbye", "see you", "see ya", "later", "cya"}
        how_are_you = {
            "how are you", "how are you doing", "how's it going",
            "hows it going", "how do you do",
        }
        if q in greetings:
            return (
                "Hi! I'm your campus assistant. I can help you find rooms, "
                "give directions, or tell you what's on a floor. What are you "
                "looking for?"
            )
        if q in thanks:
            return "You're welcome! Anything else I can help you find?"
        if q in farewells:
            return "Goodbye! Find me here whenever you need directions."
        if q in how_are_you:
            return (
                "Doing great, thanks! I'm ready to help you navigate the "
                "campus — which room or place are you after?"
            )
        return None

    def _where_am_i_intent(self, user_query: str) -> str | None:
        """Return one of 'campus' | 'building' | 'general' for the
        respective "where am I"-style questions, or None when nothing
        matches. Caught before RAG so we can answer from GPS instead
        of letting the LLM echo the question (a small-model failure
        mode when context is uninformative)."""
        q = user_query.lower()
        campus_patterns = [
            r"\b(which|what)\s+campus\s+(am\s+i\s+(in|on)|is\s+this)\b",
            r"\b(in|on)\s+(which|what)\s+campus\s+am\s+i\b",
            r"\bwhat\s+campus\s+(am\s+i|i'?m|i\s+am)\b",
        ]
        if any(re.search(p, q) for p in campus_patterns):
            return "campus"
        building_patterns = [
            r"\b(which|what)\s+building\s+(am\s+i\s+(in|on)|is\s+this|am\s+i)\b",
            r"\b(in|on)\s+(which|what)\s+building\s+am\s+i\b",
            r"\bwhat(?:'s|\s+is)\s+(this|the)\s+building(?:\s+called)?\b",
            r"\bwhat\s+building\s+is\s+this\b",
        ]
        if any(re.search(p, q) for p in building_patterns):
            return "building"
        general_patterns = [
            r"\bwhere\s+am\s+i\b",
            r"\bwhere\s+i\s+am\b",
            r"\b(what|which)\s+room\s+(am\s+i\s+(in|on)|is\s+this)\b",
            r"\bin\s+(what|which)\s+room\s+am\s+i\b",
            r"\bmy\s+(current\s+)?location\b",
            r"\blocate\s+me\b",
        ]
        if any(re.search(p, q) for p in general_patterns):
            return "general"
        return None

    def _vertical_intent(self, user_query: str) -> str | None:
        """Return 'elevator' | 'stairs' | 'any' for questions about vertical
        transport in this building, else None."""
        q = user_query.lower()
        if not re.search(
            r"\b(is|are)\s+there\b|\bwhere\b|\bhow\s+(do|can)\b|\bdoes\b|\bany\b|\bhave\b",
            q,
        ):
            return None
        if re.search(r"\b(elevator|lift)s?\b", q):
            return "elevator"
        if re.search(r"\b(stairs?|staircase|stairwell|escalator|ramp)\b", q):
            return "stairs"
        if re.search(r"\b(go\s+up|go\s+down|upstairs|downstairs|next\s+floor|another\s+floor|other\s+floor)\b", q):
            return "any"
        return None

    def _about_building_intent(self, user_query: str) -> bool:
        """True for "tell me about this building" style questions — answered
        deterministically with floor count + name. """
        q = user_query.lower()
        return bool(
            re.search(r"\btell\s+me\s+about\s+(this|the)\s+(building|place)\b", q)
            or re.search(r"\b(describe|info|information|details?)\s+(about|on|for)\s+(this|the)\s+(building|place)\b", q)
            or re.search(r"\bwhat\s+(is\s+)?(this|the)\s+(building|place)\b", q)
        )

    def _navigation_intent(self, user_query: str) -> bool:
        """True when the user is asking for a route ("how do I get to X",
        "directions to X", "take me to X"), as opposed to a listing
        ("what's on floor 1")."""
        q = user_query.lower()
        patterns = [
            r"\bhow\s+(do|can|could|would|should)\s+i\s+(get|go|reach|find|walk|navigate|head|travel)\b",
            r"\bhow\s+to\s+(get|go|reach|find|walk|navigate|head|travel)\b",
            r"\b(directions?|route|way|path)\s+(to|towards?|for)\b",
            r"\b(take|bring|guide|lead|point|show|send)\s+me\s+to\b",
            r"\b(get|go|navigate|head|walk|move)\s+to\b",
            r"\bnavigate\s+(me\s+)?to\b",
            r"\breach\s+(the|a|an|my)\b",
            r"\bfind\s+(my|the)\s+way\b",
            r"\bi\s+(want|need|'?d\s+like|would\s+like)\s+to\s+(get|go|reach|navigate|head)\b",
            r"\b(which|what)\s+way\s+(to|is)\b",
            r"\bwhere\s+do\s+i\s+go\b",
        ]
        return any(re.search(p, q) for p in patterns)

    # Lead-in phrases stripped to leave just the destination ("GR5", "cafeteria").
    _NAV_LEADS = [
        "how do i get to", "how do i go to", "how do i reach", "how do i navigate to",
        "how do i find my way to", "how can i get to", "how can i reach",
        "how to get to", "how to go to", "how to reach", "how to navigate to",
        "i want to go to", "i want to get to", "i need to go to", "i need to get to",
        "i'd like to go to", "i would like to go to", "where do i go to",
        "show me the way to", "find my way to", "the way to", "way to",
        "navigate me to", "navigate to", "directions to", "direction to",
        "route to", "path to", "take me to", "bring me to", "guide me to",
        "lead me to", "point me to", "show me to", "send me to",
        "get to", "go to", "head to", "walk to", "reach",
    ]

    def _destination_text(self, query: str) -> str:
        """Strip the navigation lead-in so we resolve just the *destination*
        ("how do I get to the cafeteria" -> "cafeteria"). Falls back to the
        whole query if nothing strips."""
        q = (query or "").strip().rstrip("?.! ").strip()
        low = q.lower()
        for p in sorted(self._NAV_LEADS, key=len, reverse=True):
            if low == p:
                return query
            if low.startswith(p + " "):
                q = q[len(p):].strip()
                low = q.lower()
                break
        for art in ("the ", "a ", "an ", "to "):
            if low.startswith(art):
                q = q[len(art):].strip()
                break
        return q or query

    def _floor_intent(self, user_query: str) -> int | None:
        """
        Returns the floor_index if the query is asking about a specific floor, else None.
        Handles ordinals (1st, 2nd, 5th), cardinals (floor 5), and named floors (ground floor).
        """
        q = user_query.lower()

        named = {
            "ground": 0, "stue": 0, "basement": -1,
            "first": 1, "second": 2, "third": 3, "fourth": 4,
            "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
        }
        for word, idx in named.items():
            if re.search(rf"\b{word}\b", q):
                return idx

        # "5th floor", "floor 5", "level 5"
        m = re.search(r"\b(\d+)(?:st|nd|rd|th)?\s+(?:floor|level)\b", q)
        if m:
            return int(m.group(1))
        m = re.search(r"\b(?:floor|level)\s+(\d+)\b", q)
        if m:
            return int(m.group(1))

        return None

    def _floor_count_intent(self, user_query: str) -> bool:
        """True for "how many floors/levels/storeys" structure questions.
        Checked before the where-am-i intent because "...in this building"
        otherwise wrongly triggers the "which building am I in" answer."""
        q = user_query.lower()
        return bool(
            re.search(r"\bhow\s+many\s+(floors?|levels?|stor(?:e?ys?|ies))\b", q)
            or re.search(r"\bnumber\s+of\s+(floors?|levels?|stor(?:e?ys?|ies))\b", q)
            or re.search(r"\b(floors?|levels?|stor(?:e?ys?|ies))\s+(does|do|are|is)\b.*\bbuilding\b", q)
        )

    def _find_place_intent(self, user_query: str) -> bool:
        """True for locate-a-place questions, answered deterministically from
        the top match so the floor is taken straight from the map data instead
        of being (mis)phrased — or invented — by the small LLM. Covers
        "where is X", "is there a X", "do you have a X", "which floor is X on"."""
        q = user_query.lower()
        # Exclude self-location ("which floor am I on"), handled separately.
        if self._which_floor_intent(q):
            return False
        return bool(
            re.search(r"\bwhere(\s+is|\s+are|'?s|\s+can\s+i\s+find)\b", q)
            or re.search(r"\blocation\s+of\b", q)
            or re.search(r"\b(is|are)\s+there\s+(a|an|any)\b", q)
            or re.search(r"\b(do|does)\s+\w+.*\bhave\s+(a|an|any)\b", q)
            or re.search(r"\b(which|what)\s+floor\s+is\b", q)
        )

    def _which_floor_intent(self, user_query: str) -> bool:
        """True for "which floor am I on" self-location questions (distinct
        from "which floor is the toilet on", which is a find-place query)."""
        q = user_query.lower()
        return bool(
            re.search(r"\b(which|what)\s+floor\b.*\b(am\s+i|i'?m|i\s+am)\b", q)
            or re.search(r"\b(am\s+i|i'?m|i\s+am)\b.*\b(which|what)\s+floor\b", q)
            or re.search(r"\b(what|which)\s+floor\s+is\s+this\b", q)
            or re.search(r"\bmy\s+(current\s+)?floor\b", q)
        )

    # Depending on the question search all the spaces or only subset of spaces
    def _needs_global_map(self, user_query: str) -> bool:
        q = user_query.lower().strip()
        if re.search(r"\b(farthest|furthest|most\s+distant|longest\s+(distance|path))\b", q):
            return True
        if re.search(r"\b(closest|nearest|shortest\s+(distance|path))\b", q):
            return True
        return False

    def _distance_intent(self, user_query: str) -> tuple[str | None, list[str] | None]:
        """
        Returns (extreme, type) where:
        - extreme: "max"|"min"|None
        """
        q = user_query.lower()

        extreme = None
        if re.search(r"\b(farthest|furthest|most\s+distant|longest\s+(distance|path))\b", q):
            extreme = "max"
        elif re.search(r"\b(closest|nearest|shortest\s+(distance|path))\b", q):
            extreme = "min"

        if not extreme:
            return None, None

        # Types can be further refined
        if re.search(r"\boffice(s)?\b", q):
            return extreme, ["ROOM_OFFICE"]
        if re.search(r"\b(classroom|lecture\s*hall|auditorium)\b", q):
            return extreme, ["ROOM_CLASSROOM", "ROOM_LECTURE_HALL", "AUDITORIUM"]
        if re.search(r"\b(restroom|toilet|bathroom|wc)\b", q):
            return extreme, ["RESTROOM", "RESTROOM_ACCESSIBLE"]
        if re.search(r"\b(cafeteria|canteen|cafe|coffee)\b", q):
            return extreme, ["CAFETERIA"]
        if re.search(r"\b(library)\b", q):
            return extreme, ["LIBRARY"]
        if re.search(r"\b(entrance|main\s+entrance|exit)\b", q):
            return extreme, ["ENTRANCE", "ENTRANCE_SECONDARY"]
        if re.search(r"\b(hallway|corridor|hall)\b", q):
            return extreme, ["CORRIDOR"]
        if re.search(r"\b(elevator|lift)s?\b", q):
            return extreme, ["ELEVATOR"]
        if re.search(r"\b(stairs?|staircase|stairwell)\b", q):
            return extreme, ["STAIRCASE", "OUTDOOR_STAIRS"]

        return extreme, None

    @staticmethod
    def _ordinal_floor_label(idx: int) -> str:
        names = {
            -1: "the basement", 0: "the ground floor", 1: "the first floor",
            2: "the second floor", 3: "the third floor", 4: "the fourth floor",
            5: "the fifth floor", 6: "the sixth floor", 7: "the seventh floor",
            8: "the eighth floor",
        }
        return names.get(idx, f"floor {idx}")

    @staticmethod
    def _transport_phrase(t: dict) -> str:
        """Natural phrase for a vertical-transport space, avoiding
        redundancy when the display name already names the type
        (e.g. "the East Elevator" not "the East Elevator elevator")."""
        name = t.get("name") or "elevator"
        low = name.lower()
        type_word = {
            "ELEVATOR": "elevator", "ESCALATOR": "escalator", "RAMP": "ramp",
        }.get(t.get("type"), "staircase")
        if any(w in low for w in ("elevator", "lift", "stair", "escalator", "ramp")):
            return f"the {name}"
        return f"the {name} {type_word}"

    def _floor_change_answer(
        self,
        *,
        campus_id: str,
        building_id: str,
        target_floor: int,
        current_space: str | None,
    ) -> Dict[str, Any]:
        """Deterministic floor-change directions grounded in the real graph."""
        transport = self.repo.vertical_transport_in_building(campus_id, building_id)
        target_label = (
            self.repo.floor_label_for_index(campus_id, target_floor, building_id)
            or self._ordinal_floor_label(target_floor)
        )
        start = f"From {current_space}, " if current_space else ""

        if not transport:
            return {
                "answer": (
                    f"I can see {target_label} in this building, but there's no "
                    f"elevator or staircase mapped here yet, so I can't give you "
                    f"a step-by-step route to it."
                ),
                "sources": [current_space] if current_space else [],
            }

        elevators = [t for t in transport if t.get("type") == "ELEVATOR"]
        stairs = [t for t in transport if t.get("type") != "ELEVATOR"]
        primary = (elevators or stairs)[0]
        alt = stairs[0] if (elevators and stairs) else None

        answer = f"{start}take {self._transport_phrase(primary)} to {target_label}."
        if alt is not None:
            answer += f" You can also use {self._transport_phrase(alt)}."

        sources = [t["name"] for t in transport[:3] if t.get("name")]
        if current_space:
            sources = [current_space] + sources
        return {"answer": answer, "sources": sources}

    @staticmethod
    def _turn_hint(a: dict, b: dict, c: dict) -> str | None:
        """Approximate left/right turn at node ``b`` for the path a→b→c, from
        local floor-plan centroids. Only meaningful when all three share a
        floor and have coordinates."""
        if (a.get("floor_index") != b.get("floor_index")
                or b.get("floor_index") != c.get("floor_index")):
            return None
        try:
            ax, ay = float(a["cx"]), float(a["cy"])
            bx, by = float(b["cx"]), float(b["cy"])
            cx, cy = float(c["cx"]), float(c["cy"])
        except (TypeError, ValueError, KeyError):
            return None
        v1x, v1y = bx - ax, by - ay
        v2x, v2y = cx - bx, cy - by
        cross = v1x * v2y - v1y * v2x
        dot = v1x * v2x + v1y * v2y
        ang = math.degrees(math.atan2(cross, dot))
        if ang > 35:
            return "turn left"
        if ang < -35:
            return "turn right"
        return None

    def _render_directions(
        self, steps: list[dict], origin_name: str | None, dest_name: str
    ) -> str:
        """Turn the ordered path nodes into spoken-style directions that
        follow the route — the same node sequence the polyline draws."""
        pieces: list[str] = []
        n = len(steps)
        for i in range(1, n):
            cur = steps[i]
            ty = (cur.get("type") or "").upper()
            name = cur.get("name") or ""
            fidx = cur.get("floor_index")
            turn = self._turn_hint(steps[i - 1], cur, steps[i + 1]) if i < n - 1 else None

            if ty.startswith("DOOR_"):
                seg = "go through the door"
            elif ty == "PASSAGE":
                seg = "continue through"
            elif ty == "STAIRCASE":
                seg = (f"take the stairs to {self._ordinal_floor_label(fidx)}"
                       if fidx is not None else "take the stairs")
            elif ty == "ELEVATOR":
                seg = (f"take the elevator to {self._ordinal_floor_label(fidx)}"
                       if fidx is not None else "take the elevator")
            elif ty == "ESCALATOR":
                seg = "take the escalator"
            elif ty == "RAMP":
                seg = "take the ramp"
            elif ty in ("CORRIDOR", "CORRIDOR_SEGMENT"):
                seg = f"continue along {name}" if name else "continue along the corridor"
            elif ty == "LOBBY":
                seg = f"cross {name}" if name else "cross the lobby"
            elif i == n - 1:
                seg = f"arrive at {name or dest_name}"
            else:
                seg = f"pass {name}" if name else None

            if not seg:
                continue
            if turn and ty in ("CORRIDOR", "CORRIDOR_SEGMENT"):
                seg = f"{turn}, then {seg}"
            elif turn and ty.startswith("DOOR_"):
                seg = f"{turn} and {seg}"
            pieces.append(seg)

        if not pieces or not pieces[-1].startswith("arrive"):
            pieces.append(f"arrive at {dest_name}")
        if len(pieces) > 8:  # keep the answer readable
            pieces = pieces[:7] + [pieces[-1]]
        start = f"From {origin_name}, " if origin_name else "From your current spot, "
        return start + "; ".join(pieces) + "."

    async def _directions_answer(
        self,
        *,
        user_query: str,
        campus_id: str,
        building_id: str | None,
        loc: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        """Answer "how do I get to X" for a place (not a floor): resolve the
        destination space, then give route-following directions when we know
        where the user is, otherwise say where it is."""
        # Resolve the *destination*: strip the "how do I get to…" lead-in so we
        # embed/match just the place name, and DON'T GPS-radius-filter it — the
        # destination can be anywhere in the building, not near the user.
        dest_q = self._destination_text(user_query)
        query_vector = await self._encode_query(dest_q)

        def _resolve(bid: str | None) -> dict | None:
            sims = self.repo.search_similar_spaces(
                campus_id, query_vector, 30, bid, None, None, None,
            )
            top = sims[0] if sims else None
            if top is not None and (top.get("score") or 0.0) >= _FIND_PLACE_HIGH_SCORE:
                return top
            for cand in sims:
                if _find_place_token_match(dest_q, cand.get("name"), cand.get("type")):
                    return cand
            return None

        chosen = await asyncio.to_thread(_resolve, building_id)
        if chosen is None and building_id is not None:
            chosen = await asyncio.to_thread(_resolve, None)  # widen to whole campus

        if chosen is None:
            return {
                "answer": (
                    f"I couldn't find \"{dest_q}\" on this campus. "
                    f"Try the room name or number — e.g. \"how do I get to GR5?\""
                ),
                "sources": [],
            }

        dest_name = chosen.get("name") or "your destination"
        floor = _floor_label(chosen.get("floor_name"), chosen.get("floor_index"))
        building = chosen.get("building_name") or "the building"
        neighbours = [n for n in (chosen.get("connected_to") or []) if n][:2]
        near = f", near {', '.join(neighbours)}" if neighbours else ""

        origin_id = loc.get("space_id") if loc else None
        dest_id = chosen.get("id")

        if origin_id and dest_id and origin_id != dest_id:
            steps = await asyncio.to_thread(
                self.repo.route_between, campus_id, origin_id, dest_id,
            )
            if steps and len(steps) >= 2:
                directions = self._render_directions(
                    steps, loc.get("name") if loc else None, dest_name,
                )
                print(f"[chat] directions: {len(steps)} steps to {dest_name!r}", flush=True)
                return {"answer": directions, "sources": [dest_name]}

        # No usable origin / no path: say where it is + how to get a live route.
        print(f"[chat] directions: no route (origin={origin_id!r}) -> location hint", flush=True)
        return {
            "answer": (
                f"{dest_name} is on {floor} of {building}{near}. "
                f"Open it on the map and tap Navigate for a step-by-step route."
            ),
            "sources": [dest_name],
        }

    async def chat(
        self,
        user_query: str,
        campus_id: str,
        building_id: str | None = None,
        user_lat: float | None = None,
        user_lon: float | None = None,
        floor_index: int | None = None,
        current_location_space_id: str | None = None,
    ) -> Dict[str, Any]:
        t0 = time.perf_counter()

        print(
            f"[chat] received: query={user_query!r} campus={campus_id!r} "
            f"ios_building={building_id!r} gps=({user_lat}, {user_lon})",
            flush=True,
        )

        smalltalk = self._smalltalk_reply(user_query)
        if smalltalk is not None:
            print("[chat] smalltalk → canned reply", flush=True)
            return {"answer": smalltalk, "sources": []}

        # Prefer a forced / landmark snap (the red dot) over GPS. Indoors GPS
        # drifts, so when the client has snapped the user to a space, "where am
        # I" must report that room — not whatever GPS is nearest.
        loc = None
        if current_location_space_id:
            loc = self.repo.get_space_location(campus_id, current_location_space_id)
        if loc is None and user_lat is not None and user_lon is not None:
            loc = self.repo.locate_user(campus_id, user_lat, user_lon)
        user_building_id = loc.get("building_id") if loc else None
        user_building_name = loc.get("building_name") if loc else None

        print(
            f"[chat] locate_user: building={user_building_name!r} "
            f"building_id={user_building_id!r} "
            f"inside={loc.get('inside') if loc else None} "
            f"nearest_space={loc.get('name') if loc else None}",
            flush=True,
        )

        effective_building_id = user_building_id or building_id

        def _building_name_for(bid: str | None) -> str:
            if user_building_name:
                return user_building_name
            if bid:
                floors = self.repo.list_building_floors(campus_id, bid)
                if floors and floors[0].get("building_name"):
                    return floors[0]["building_name"]
            return "this building"

        # Building-structure questions ("how many floors?") are answered
        # deterministically from the graph. Checked BEFORE the where-am-i
        # intent, whose "...this building" pattern would otherwise hijack them.
        if self._floor_count_intent(user_query):
            if not effective_building_id:
                return {
                    "answer": "I'm not sure which building you mean — open it on the map or get closer so I can tell.",
                    "sources": [],
                }
            floors = self.repo.list_building_floors(campus_id, effective_building_id)
            if not floors:
                return {"answer": "I don't have floor information for this building.", "sources": []}
            bname = floors[0].get("building_name") or "this building"
            labels = [
                (f.get("floor_name") or self._ordinal_floor_label(f.get("floor_index")))
                for f in floors
            ]
            if len(labels) == 1:
                readable = labels[0]
            else:
                readable = ", ".join(labels[:-1]) + " and " + labels[-1]
            n = len(floors)
            print(f"[chat] floor-count: building={bname!r} floors={n}", flush=True)
            return {
                "answer": f"{bname} has {n} floor{'s' if n != 1 else ''}: {readable}.",
                "sources": [bname],
            }

        # "Which floor am I on?" — answered from the floor the app is showing
        # (the manually selected floor, or the barometer-derived one when none
        # was picked), falling back to the nearest space's floor from GPS.
        if self._which_floor_intent(user_query):
            idx = floor_index
            label = None
            if idx is not None:
                label = (
                    self.repo.floor_label_for_index(campus_id, idx, effective_building_id)
                    or self._ordinal_floor_label(idx)
                )
            elif loc and loc.get("floor_name"):
                label = loc.get("floor_name")
            if not label:
                return {
                    "answer": "I don't know which floor you're on yet — pick a floor on the map, or move so the barometer can detect it.",
                    "sources": [],
                }
            building = user_building_name or "this building"
            print(f"[chat] which-floor: floor_index={floor_index} label={label!r}", flush=True)
            return {"answer": f"You're on {label} of {building}.", "sources": [label]}

        if self._about_building_intent(user_query) and effective_building_id:
            floors = self.repo.list_building_floors(campus_id, effective_building_id)
            bname = (
                (floors[0].get("building_name") if floors else None)
                or _building_name_for(effective_building_id)
            )
            if floors:
                n = len(floors)
                labels = [
                    (f.get("floor_name") or self._ordinal_floor_label(f.get("floor_index")))
                    for f in floors
                ]
                readable = labels[0] if n == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
                ans = (
                    f"{bname} has {n} floor{'s' if n != 1 else ''}: {readable}."
                )
                return {"answer": ans, "sources": [bname]}
            return {"answer": f"I don't have details for {bname} yet.", "sources": []}

        vintent = self._vertical_intent(user_query)
        if vintent is not None:
            if not effective_building_id:
                return {
                    "answer": "I'm not sure which building you mean — open it on the map or get closer so I can check.",
                    "sources": [],
                }
            transports = self.repo.vertical_transport_in_building(campus_id, effective_building_id)
            if vintent == "elevator":
                wanted_types = {"ELEVATOR"}
                label = "elevator"
            elif vintent == "stairs":
                wanted_types = {"STAIRCASE", "OUTDOOR_STAIRS", "ESCALATOR", "RAMP"}
                label = "stairs"
            else:
                wanted_types = {"ELEVATOR", "STAIRCASE", "OUTDOOR_STAIRS", "ESCALATOR", "RAMP"}
                label = "elevator or stairs"
            matching = [t for t in transports if (t.get("type") in wanted_types)]
            bname = _building_name_for(effective_building_id)
            if not matching:
                return {
                    "answer": f"I don't see any {label} mapped in {bname}.",
                    "sources": [],
                }
            names: list[str] = []
            seen_lower: set[str] = set()
            for t in matching:
                nm = t.get("name")
                if not nm:
                    continue
                key = nm.lower()
                if key in seen_lower:
                    continue
                seen_lower.add(key)
                names.append(nm)
            if len(names) == 1:
                ans = f"Yes — {names[0]} is in {bname}."
            else:
                listed = ", ".join(names[:-1]) + " and " + names[-1]
                ans = f"Yes — {bname} has {listed}."
            return {"answer": ans, "sources": names[:3]}

        loc_intent = self._where_am_i_intent(user_query)
        print(f"[chat] intent: {loc_intent!r}", flush=True)
        if loc_intent is not None:
            # A forced/landmark snap gives us `loc` even without GPS, so gate on
            # `loc` (not on GPS) — otherwise we'd reject a known red-dot position.
            if not loc:
                return {
                    "answer": "I don't have your location yet — enable location, or point your camera at a landmark to set it.",
                    "sources": [],
                }
            building = loc.get("building_name") or "the building"
            floor = loc.get("floor_name")
            dist = int(round(loc["distance_m"]))

            if loc_intent == "campus":
                campus_name = self.repo.get_campus_name(campus_id) or "this campus"
                if loc["inside"]:
                    ans = f"You're in {campus_name}, inside {building}."
                else:
                    ans = f"You're on {campus_name}, near {building} (about {dist} m away)."
                return {"answer": ans, "sources": [campus_name]}

            if loc_intent == "building":
                if loc["inside"]:
                    ans = f"You're in {building}."
                else:
                    ans = f"You're near {building} (about {dist} m away)."
                return {"answer": ans, "sources": [building]}

            floor_part = f" on {floor}" if floor else ""
            if loc["inside"]:
                ans = f"You're in {loc['name']}{floor_part} in {building}."
            else:
                ans = f"You're near {loc['name']}{floor_part} in {building} (about {dist} m away)."
            return {"answer": ans, "sources": [loc["name"]]}

        # "Closest/farthest X" anchored on the main entrance only makes sense
        # WITHOUT a user position. When we know where the user is, a query like
        # "where is the nearest toilet" should be answered relative to them, so
        # we let it fall through to the find-place path (which filters spaces by
        # a radius around the user's GPS).
        have_gps = user_lat is not None and user_lon is not None
        if self._needs_global_map(user_query) and not have_gps:
            extreme, candidate_types = self._distance_intent(user_query)

            # Anchor selection is modular and based on building data
            anchor = self.repo.get_anchor_space(
                campus_id,
                space_types=["ENTRANCE", "LOBBY", "ENTRANCE_SECONDARY"],
                name_keywords=["main", "entrance", "front"],
                tag_keywords=["main_entrance", "main entrance", "entrance"],
            )

            if extreme and anchor and candidate_types:
                res = self.repo.extreme_space_by_distance(
                    campus_id,
                    anchor_space_id=anchor["id"],
                    candidate_space_types=candidate_types,
                    extreme=extreme,
                )
            else:
                res = None

            if res:
                verb = "farthest" if extreme == "max" else "closest"
                target_label = "space"
                # To fix, make it work for all types, not just offices
                if candidate_types == ["ROOM_OFFICE"]:
                    target_label = "office"
                return {
                    "answer": f"The {verb} {target_label} from the main entrance is {res['target_name']}.",
                    "sources": [res.get("anchor_name", "Main entrance"), res["target_name"]],
                }

        nav = self._navigation_intent(user_query)
        floor_idx = self._floor_intent(user_query)

        if nav and floor_idx is not None and effective_building_id:
            print(f"[chat] floor-change nav: target={floor_idx} -> grounded answer", flush=True)
            return self._floor_change_answer(
                campus_id=campus_id,
                building_id=effective_building_id,
                target_floor=floor_idx,
                current_space=(loc.get("name") if loc else None),
            )

        # "How do I get to <place>?" — resolve the destination and give
        # route-following directions (instead of the generic fallback).
        if nav:
            print("[chat] navigation intent -> directions", flush=True)
            return await self._directions_answer(
                user_query=user_query,
                campus_id=campus_id,
                building_id=effective_building_id,
                loc=loc,
            )

        if floor_idx is not None and not nav:
            spaces = self.repo.search_spaces_on_floor(
                campus_id, floor_idx, limit=20, building_id=building_id,
            )
            # No spaces on that floor (or the floor doesn't exist) -> refuse.
            if not spaces:
                print(f"[chat] floor-listing: no spaces on floor {floor_idx} -> refuse", flush=True)
                return {"answer": "I don't have that information.", "sources": []}
            similar_spaces = spaces
            top_in = None
            in_score = 0.0
        else:
            query_vector = await self._encode_query(user_query)
            radius_m = 200.0 if (user_lat is not None and user_lon is not None) else None
            top_k = 30 if self._find_place_intent(user_query) else 10

            similar_spaces = await asyncio.to_thread(
                self.repo.search_similar_spaces,
                campus_id, query_vector, top_k, effective_building_id,
                user_lat, user_lon, radius_m,
            )

            _WRONG_BLDG_FLOOR = 0.50
            _WRONG_BLDG_GAP = 0.15

            top_in = similar_spaces[0] if similar_spaces else None
            in_score = (top_in.get("score") if top_in else 0.0) or 0.0
            cross_score = 0.0
            top_cross = None

            if user_building_name and user_building_id:
                cross_campus = await asyncio.to_thread(
                    self.repo.search_similar_spaces,
                    campus_id, query_vector, 3, None,
                    None, None, None,
                )
                top_cross = cross_campus[0] if cross_campus else None
                cross_score = (top_cross.get("score") if top_cross else 0.0) or 0.0

                if (
                    cross_score >= _WRONG_BLDG_FLOOR
                    and cross_score - in_score >= _WRONG_BLDG_GAP
                    and top_cross
                    and top_cross.get("building_name")
                    and top_cross["building_name"] != user_building_name
                ):
                    print(
                        f"[chat] wrong-building: query={user_query!r} "
                        f"in_score={in_score:.3f} cross_score={cross_score:.3f} "
                        f"user_building={user_building_name!r} "
                        f"answer_building={top_cross['building_name']!r}",
                        flush=True,
                    )
                    return {
                        "answer": (
                            f"{top_cross['name']} is in "
                            f"{top_cross['building_name']}, but you're "
                            f"currently in {user_building_name}. You'll need "
                            f"to head over to {top_cross['building_name']} "
                            f"first."
                        ),
                        "sources": [
                            top_cross["name"],
                            top_cross["building_name"],
                        ],
                    }

            print(
                f"[chat] rag: query={user_query!r} "
                f"user_building={user_building_name!r} "
                f"in_results={len(similar_spaces)} "
                f"in_top_score={in_score:.3f} "
                f"cross_top_score={cross_score:.3f}",
                flush=True,
            )

            # "Where is X" / "is there a X" questions are answered
            # deterministically. 
            if self._find_place_intent(user_query):

                chosen: dict | None = None
                if top_in is not None and in_score >= _FIND_PLACE_HIGH_SCORE:
                    chosen = top_in
                else:

                    for cand in similar_spaces:
                        if _find_place_token_match(
                            user_query, cand.get("name"), cand.get("type"),
                        ):
                            chosen = cand
                            break

                if chosen is not None:
                    floor = _floor_label(chosen.get("floor_name"), chosen.get("floor_index"))
                    building = chosen.get("building_name") or "the building"
                    neighbours = [n for n in (chosen.get("connected_to") or []) if n][:3]
                    near = f", near {', '.join(neighbours)}" if neighbours else ""
                    name = chosen.get("name") or "It"
                    sc = float(chosen.get("score") or 0.0)
                    print(
                        f"[chat] find-place: {name!r} score={sc:.3f} "
                        f"top_was={(top_in or {}).get('name')!r}",
                        flush=True,
                    )
                    return {
                        "answer": f"{name} is on {floor} of {building}{near}.",
                        "sources": [name],
                    }
                print(
                    f"[chat] find-place: refuse "
                    f"(top_score={in_score:.3f} high={_FIND_PLACE_HIGH_SCORE} "
                    f"low={_FIND_PLACE_LOW_SCORE} top_name={(top_in or {}).get('name')!r})",
                    flush=True,
                )
                return {"answer": "I don't have that information.", "sources": []}


        if floor_idx is not None and not nav:
            floor_label_str = (
                (similar_spaces[0].get("floor_name") if similar_spaces else None)
                or self._ordinal_floor_label(floor_idx)
            )
            building_label = (
                (similar_spaces[0].get("building_name") if similar_spaces else None)
                or _building_name_for(effective_building_id)
            )

            hidden_types = {
                "DOOR_STANDARD", "DOOR_AUTOMATIC", "DOOR_LOCKED",
                "DOOR_EMERGENCY", "PASSAGE", "CONNECTOR",
            }
            visible = [
                s for s in similar_spaces
                if (s.get("type") or "") not in hidden_types and s.get("name")
            ]
            names = []
            for s in visible:
                nm = s.get("name")
                if nm and nm not in names:
                    names.append(nm)
            if not names:
                return {"answer": "I don't have that information.", "sources": []}
            shown = names[:8]
            extra = len(names) - len(shown)
            listed = ", ".join(shown[:-1]) + (
                f" and {shown[-1]}" if len(shown) > 1 else shown[0]
            )
            extra_phrase = f" (and {extra} more)" if extra > 0 else ""
            return {
                "answer": f"{floor_label_str} of {building_label} has {listed}{extra_phrase}.",
                "sources": shown[:5],
            }


        mode = (os.getenv("ASSISTANT_MODE") or "offline").strip().lower()
        use_llm = mode == "online" and bool(similar_spaces)
        if not use_llm:
            print(
                f"[chat] no deterministic intent matched -> refuse "
                f"(top_score={in_score:.3f}, mode={mode})",
                flush=True,
            )
            return {
                "answer": (
                    "I can answer questions about rooms, floors, navigation, "
                    "and where things are in a building. Try \"where is the "
                    "library?\", \"how many floors are here?\", or \"is there "
                    "an elevator?\"."
                ),
                "sources": [],
            }

        context_lines = []
        for s in similar_spaces:
            floor = _floor_label(s.get("floor_name"), s.get("floor_index"))
            building = s.get("building_name") or "the building"
            location = f"on {floor} of {building}"
            neighbours = [n for n in (s.get("connected_to") or []) if n]
            if neighbours:
                near = ", ".join(neighbours[:5])
                graph_context = f", near {near}"
            else:
                graph_context = ""
            context_lines.append(f"- {s.get('name','?')} is {location}{graph_context}.")
        context_text = "\n".join(context_lines)

        prompt_path = Path(__file__).resolve().parents[1] / "core" / "prompt" / "prompt.txt"
        system_prompt = prompt_path.read_text(encoding="utf-8")

        tokenizer, _, _ = get_llm()
        context_text, user_query_trim = _truncate_context(
            tokenizer,
            prefix=f"Context:\n{context_text}",
            question=f"Question: {user_query}",
            max_input_tokens=1200,
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"{context_text}\n\n{user_query_trim}"},
        ]
        response = await asyncio.to_thread(_generate_sync, messages)
        return {
            "answer": response,
            "sources": [s.get("name", "?") for s in similar_spaces],
        }
