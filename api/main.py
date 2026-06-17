from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.db.connection import create_pool, close_pool
from api.routes.bankstatement import router as bankstatement_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_pool()
    yield
    await close_pool()


app = FastAPI(
    title="Bank Statement AI API",
    version="1.0.0",
    description="Agentic PDF → Excel extraction with per-task Docker isolation",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bankstatement_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
