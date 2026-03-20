from contextlib import asynccontextmanager

from fastapi import FastAPI

from db import get_db
from routes import assistant, embed


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = get_db()
    yield
    db.close()


app = FastAPI(
    title="Indoor Navigation Assistant",
    version="1.0.0",
    lifespan=lifespan,
)

PREFIX = "/api/v1"

app.include_router(assistant.router, prefix=PREFIX)
app.include_router(embed.router)


@app.get("/health")
def health():
    return {"status": "ok"}
