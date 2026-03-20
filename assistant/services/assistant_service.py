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

    text = re.sub(r"^\d+\.\s.*?\n", "", text, flags=re.MULTILINE)

    text = text.strip()
    if "." in text:
        text = text.split(".")[0] + "."

    return text.replace("\n", " ").strip()


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
    def __init__(self, db):
        self.repo = AssistantRepository(db)

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

    async def chat(self, user_query: str, campus_id: str) -> Dict[str, Any]:
        t0 = time.perf_counter()

        # Determine if user requested a global map question
        if self._needs_global_map(user_query):
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

        query_vector = await self._encode_query(user_query)

        similar_spaces = self.repo.search_similar_spaces(campus_id, query_vector, limit=10)

        context_lines = []
        for s in similar_spaces:
            location = f"located on {s.get('floor_name', 'an unknown floor')} in the {s.get('building_name', 'unknown building')}."
            connections = s.get('connected_to', [])
            if connections:
                conn_show = connections[:6]
                conn_str = ", ".join([f"{c.get('name', '?')} (via {c.get('connection_type', '?')})" for c in conn_show])
                more = "" if len(connections) <= len(conn_show) else f" (+{len(connections)-len(conn_show)} more)"
                graph_context = f" It directly connects to: {conn_str}{more}."
            else:
                graph_context = " It has no mapped connections."
            context_lines.append(f"- {s.get('name','?')} ({s.get('type','space')}) is {location}{graph_context}")
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
