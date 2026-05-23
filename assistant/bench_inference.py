from __future__ import annotations

import os
import statistics
import time

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig

MODEL = os.getenv("ASSISTANT_OFFLINE_MODEL_ID", "HuggingFaceTB/SmolLM2-360M-Instruct")
N = 8

PROMPT = [
    {"role": "system", "content": "You are a strict indoor navigation assistant. Answer only from context."},
    {"role": "user", "content": "Context:\n- Bathroom is on the ground floor of Demo Building, "
     "near Entrance Hallway.\n\nQuestion: where is the toilet?"},
]


def bench_llm(device, dtype):
    tok = AutoTokenizer.from_pretrained(MODEL, use_fast=True)
    m = AutoModelForCausalLM.from_pretrained(MODEL, dtype=dtype, low_cpu_mem_usage=True).to(device)
    m.eval()
    torch.set_grad_enabled(False)
    gc = GenerationConfig(max_new_tokens=150, do_sample=False, use_cache=True,
                          pad_token_id=tok.eos_token_id, eos_token_id=tok.eos_token_id,
                          repetition_penalty=1.2)
    text = tok.apply_chat_template(PROMPT, tokenize=False, add_generation_prompt=True)
    inputs = tok(text, return_tensors="pt").to(device)
    with torch.inference_mode():
        _ = m.generate(**inputs, generation_config=gc)  # warm-up
    if device == "cuda":
        torch.cuda.synchronize()
    lat, tps = [], []
    for _ in range(N):
        t = time.perf_counter()
        with torch.inference_mode():
            out = m.generate(**inputs, generation_config=gc)
        if device == "cuda":
            torch.cuda.synchronize()
        dt = time.perf_counter() - t
        n_new = out.shape[1] - inputs["input_ids"].shape[1]
        lat.append(dt)
        tps.append(n_new / dt)
    del m
    if device == "cuda":
        torch.cuda.empty_cache()
    return statistics.mean(lat) * 1000, statistics.median(lat) * 1000, statistics.mean(tps)


def bench_embedder(device):
    from sentence_transformers import SentenceTransformer
    e = SentenceTransformer("all-MiniLM-L6-v2", device=device)
    e.encode(["warm up"])
    t = time.perf_counter()
    for _ in range(20):
        e.encode(["where is the nearest toilet"])
    return (time.perf_counter() - t) / 20 * 1000


def bench_spatial():
    """pgvector cosine + spatial retrieval, as the RAG path runs it."""
    try:
        from sentence_transformers import SentenceTransformer
        from db_pg import get_pg_db
        from sqlalchemy import text
        e = SentenceTransformer("all-MiniLM-L6-v2",
                                device="cuda" if torch.cuda.is_available() else "cpu")
        vec = e.encode(["where is the toilet"])[0]
        lit = "[" + ",".join(f"{float(v):.7f}" for v in vec) + "]"
        pg = get_pg_db()
        sql = text("""
            SELECT bs.display_name, 1.0 - (bs.embedding <=> CAST(:q AS vector)) AS score
            FROM building_spaces bs
            WHERE bs.campus_id = 'campus-demo' AND bs.embedding IS NOT NULL
            ORDER BY bs.embedding <=> CAST(:q AS vector) LIMIT 10
        """)
        with pg.SessionLocal() as s:
            s.execute(sql, {"q": lit}).all()  # warm
            t = time.perf_counter()
            for _ in range(20):
                s.execute(sql, {"q": lit}).all()
            return (time.perf_counter() - t) / 20 * 1000
    except Exception as exc:  # noqa: BLE001
        return f"n/a ({type(exc).__name__})"


def bench_neo4j():
    try:
        from db import get_db
        db = get_db()
        db.execute("MATCH (s:Space) WHERE s.campus_id='campus-demo' RETURN count(s) AS n")  # warm
        t = time.perf_counter()
        for _ in range(20):
            db.execute("MATCH (s:Space) WHERE s.campus_id='campus-demo' RETURN count(s) AS n")
        return (time.perf_counter() - t) / 20 * 1000
    except Exception as exc:  # noqa: BLE001
        return f"n/a ({type(exc).__name__})"


def main():
    print(f"torch {torch.__version__}  cuda_available={torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"gpu: {torch.cuda.get_device_name(0)}")

    print("\n=== LLM inference: SmolLM2-360M, 150 max_new_tokens, greedy ===")
    rows = {}
    if torch.cuda.is_available():
        rows["GPU"] = bench_llm("cuda", torch.float16)
    rows["CPU"] = bench_llm("cpu", torch.float32)
    for k, (mean, med, tps) in rows.items():
        print(f"  {k:4}  mean={mean:.0f}ms  median={med:.0f}ms  {tps:.1f} tok/s")
    if "GPU" in rows and "CPU" in rows:
        print(f"  speedup (median): {rows['CPU'][1] / rows['GPU'][1]:.1f}x")

    print("\n=== embedder encode (all-MiniLM-L6-v2, single query) ===")
    if torch.cuda.is_available():
        print(f"  GPU  {bench_embedder('cuda'):.1f}ms")
    print(f"  CPU  {bench_embedder('cpu'):.1f}ms")

    print("\n=== spatial pgvector retrieval (top-10, campus-demo) ===")
    print(f"  {bench_spatial()}")
    print("\n=== Neo4j count query (campus-demo) ===")
    print(f"  {bench_neo4j()}")


if __name__ == "__main__":
    main()
