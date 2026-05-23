import os
import time
import asyncio
import functools
from typing import Dict, Any, List, Tuple
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig
from sentence_transformers import SentenceTransformer
from repositories.assistant_repo import AssistantRepository
import re
from pathlib import Path

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

# Minumum confidence threshold
_FIND_PLACE_MIN_SCORE = 0.30


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
            r"\bwhat\s+campus\b.*\bi\b",
        ]
        if any(re.search(p, q) for p in campus_patterns):
            return "campus"
        building_patterns = [
            r"\b(which|what)\s+building\s+(am\s+i\s+(in|on)|is\s+this|am\s+i)\b",
            r"\b(in|on)\s+(which|what)\s+building\s+am\s+i\b",
            r"\bwhat'?s?\s+(this|the)\s+building\b",
            r"\bbuilding\b.*\b(am\s+i|i\s+am|i'?m|this|here)\b",
            r"\b(am\s+i|i\s+am|i'?m|this|here)\b.*\bbuilding\b",
        ]
        if any(re.search(p, q) for p in building_patterns):
            return "building"
        general_patterns = [
            r"\bwhere\s+am\s+i\b",
            r"\bwhere\s+i\s+am\b",
            r"\b(what|which)\s+room\s+(am\s+i\s+in|is\s+this)\b",
            r"\bin\s+(what|which)\s+room\s+am\s+i\b",
            r"\bmy\s+(current\s+)?location\b",
            r"\blocate\s+me\b",
            r"\bam\s+i\s+in\b",
        ]
        if any(re.search(p, q) for p in general_patterns):
            return "general"
        return None

    def _navigation_intent(self, user_query: str) -> bool:
        """True when the user is asking for a route ("how do I get to X",
        "directions to X", "take me to X"), as opposed to a listing
        ("what's on floor 1")."""
        q = user_query.lower()
        patterns = [
            r"\bhow\s+(do|can|would)\s+i\s+(get|go|reach|find|walk|navigate)\b",
            r"\bhow\s+to\s+(get|go|reach|find|walk|navigate)\b",
            r"\b(directions?|route|way)\s+to\b",
            r"\b(take|bring|guide|lead)\s+me\s+to\b",
            r"\b(get|go|navigate)\s+to\s+the\b",
            r"\breach\s+the\b",
            r"\bfind\s+my\s+way\b",
        ]
        return any(re.search(p, q) for p in patterns)

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
        if re.search(r"\b(restroom|toilet|bathroom)\b", q):
            return extreme, ["RESTROOM", "RESTROOM_ACCESSIBLE"]
        if re.search(r"\b(cafeteria|canteen)\b", q):
            return extreme, ["CAFETERIA"]
        if re.search(r"\b(library)\b", q):
            return extreme, ["LIBRARY"]
        if re.search(r"\b(entrance|main\s+entrance)\b", q):
            return extreme, ["ENTRANCE", "ENTRANCE_SECONDARY"]

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

    async def chat(
        self,
        user_query: str,
        campus_id: str,
        building_id: str | None = None,
        user_lat: float | None = None,
        user_lon: float | None = None,
        floor_index: int | None = None,
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

        loc = None
        if user_lat is not None and user_lon is not None:
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

        loc_intent = self._where_am_i_intent(user_query)
        print(f"[chat] intent: {loc_intent!r}", flush=True)
        if loc_intent is not None:
            if user_lat is None or user_lon is None:
                return {
                    "answer": "I don't have your location yet — please make sure location is enabled in the app.",
                    "sources": [],
                }
            if not loc:
                return {
                    "answer": "I can't see any rooms near you on this campus.",
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

        if floor_idx is not None and not nav:
            spaces = self.repo.search_spaces_on_floor(
                campus_id, floor_idx, limit=20, building_id=building_id,
            )
            # No spaces on that floor (or the floor doesn't exist) -> refuse
            # deterministically. Handing "No spaces found" to the LLM let it
            # fabricate (e.g. a basement morgue on a non-existent floor).
            if not spaces:
                print(f"[chat] floor-listing: no spaces on floor {floor_idx} -> refuse", flush=True)
                return {"answer": "I don't have that information.", "sources": []}
            floor_label = spaces[0].get("floor_name") or f"floor {floor_idx}"
            building_label = spaces[0].get("building_name", "the building")
            context_lines = [
                f"The following spaces are on {floor_label} (floor index {floor_idx}) in {building_label}:"
            ]
            for s in spaces:
                context_lines.append(f"- {s.get('name', '?')} ({s.get('type', 'space')})")
            context_text = "\n".join(context_lines)
            similar_spaces = spaces
        else:
            query_vector = await self._encode_query(user_query)
            radius_m = 200.0 if (user_lat is not None and user_lon is not None) else None

            similar_spaces = await asyncio.to_thread(
                self.repo.search_similar_spaces,
                campus_id, query_vector, 10, effective_building_id,
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
            # deterministically. A confident match (cosine >= 0.30) is reported
            # straight from the map data, so the small LLM can't mis-state the
            # floor. Crucially, when NO match clears the threshold we refuse
            # here — never handing an empty context to the LLM, which would
            # otherwise fabricate a plausible room (the hallucination guard).
            if self._find_place_intent(user_query):
                if top_in and in_score >= _FIND_PLACE_MIN_SCORE:
                    floor = _floor_label(top_in.get("floor_name"), top_in.get("floor_index"))
                    building = top_in.get("building_name") or "the building"
                    neighbours = [n for n in (top_in.get("connected_to") or []) if n][:3]
                    near = f", near {', '.join(neighbours)}" if neighbours else ""
                    name = top_in.get("name") or "It"
                    print(f"[chat] find-place: {name!r} floor_idx={top_in.get('floor_index')}", flush=True)
                    return {
                        "answer": f"{name} is on {floor} of {building}{near}.",
                        "sources": [name],
                    }
                print(
                    f"[chat] find-place: no confident match "
                    f"(top_score={in_score:.3f} < {_FIND_PLACE_MIN_SCORE}) -> refuse",
                    flush=True,
                )
                return {"answer": "I don't have that information.", "sources": []}

            context_lines = []
            for s in similar_spaces:
                floor = _floor_label(s.get("floor_name"), s.get("floor_index"))
                building = s.get("building_name") or "the building"
                location = f"on {floor} of {building}"
                # connected_to is now a list of real neighbour names (doors and
                # other connectors are filtered out and hopped through upstream).
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
            {"role": "user", "content": f"{context_text}\n\n{user_query_trim}"}
        ]

        response = await asyncio.to_thread(_generate_sync, messages)

        return {
            "answer": response,
            "sources": [s.get('name', '?') for s in similar_spaces]
        }
