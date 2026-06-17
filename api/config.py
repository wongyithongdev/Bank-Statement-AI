from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    postgres_host: str = "10.0.10.63"
    postgres_port: int = 5432
    postgres_db: str = "root"
    postgres_user: str = "root"
    postgres_password: str = "root"

    auth_server_url: str = "http://auth2.my365biz.com"

    mimo_api_key: str = ""
    mimo_base_url: str = "https://token-plan-sgp.xiaomimimo.com/v1"
    mimo_generator_model: str = "mimo-v2.5-pro"
    mimo_evaluator_model: str = "mimo-v2.5-pro"

    worker_image: str = "bankstatement-worker:latest"
    data_dir: str = "/data/bankstatement"

    class Config:
        env_file = str(Path(__file__).parent.parent / ".env")
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
