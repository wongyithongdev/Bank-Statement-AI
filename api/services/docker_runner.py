"""
Starts an isolated Docker worker container per bank statement task.
Each container runs the full Generator → Evaluator pipeline for one task.
"""
import docker
import logging
from api.config import settings

logger = logging.getLogger(__name__)

_docker_client: docker.DockerClient | None = None


def get_docker_client() -> docker.DockerClient:
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client


def start_worker(task_id: str) -> str:
    """
    Start a worker container for the given task_id.
    Returns the container ID.
    Raises RuntimeError if the container fails to start.
    """
    client = get_docker_client()

    env = {
        "TASK_ID": task_id,
        "POSTGRES_HOST": settings.postgres_host,
        "POSTGRES_PORT": str(settings.postgres_port),
        "POSTGRES_DB": settings.postgres_db,
        "POSTGRES_USER": settings.postgres_user,
        "POSTGRES_PASSWORD": settings.postgres_password,
        "MIMO_API_KEY": settings.mimo_api_key,
        "MIMO_BASE_URL": settings.mimo_base_url,
        "MIMO_GENERATOR_MODEL": settings.mimo_generator_model,
        "MIMO_EVALUATOR_MODEL": settings.mimo_evaluator_model,
        "DATA_DIR": settings.data_dir,
    }

    try:
        container = client.containers.run(
            image=settings.worker_image,
            detach=True,
            remove=True,
            network_mode="host",  # reach postgres at 10.0.10.63 directly
            environment=env,
            volumes={
                settings.data_dir: {"bind": settings.data_dir, "mode": "rw"},
            },
            mem_limit="2g",
            cpu_quota=100000,  # 1 CPU core
            name=f"bankstatement-worker-{task_id[:8]}",
        )
        logger.info("Started worker container %s for task %s", container.id[:12], task_id)
        return container.id
    except docker.errors.ImageNotFound:
        raise RuntimeError(
            f"Worker image '{settings.worker_image}' not found. "
            "Run: docker build -f worker/Dockerfile -t bankstatement-worker ."
        )
    except docker.errors.APIError as exc:
        raise RuntimeError(f"Docker API error: {exc}")
