import os
from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.log import default_filter, default_format, logger

SOURCE = Path(__file__).resolve().parent
ROOT = SOURCE.parent


def main() -> None:
    from src.plugins.persona_agent.config import load_config, validate_runtime_layout
    from src.plugins.persona_agent.store import SCHEMA_VERSION

    os.umask(0o077)
    config_path = Path(os.environ.get("PERSONA_AGENT_CONFIG", "config/persona_agent.yaml"))
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    config_path = config_path.resolve()
    os.environ["PERSONA_AGENT_CONFIG"] = str(config_path)
    connection_env = ROOT / "config/nonebot.env"
    for path in (connection_env, config_path, config_path.parent / "persona_agent.env"):
        if not path.is_file():
            raise FileNotFoundError(
                f"Missing configuration: {path}. Initialize a new instance or restore its configuration."
            )
    if SCHEMA_VERSION != 5:
        raise RuntimeError(
            "This deployment requires schema 5 with local vector memory and forgetting-ledger replay"
        )
    validate_runtime_layout(load_config(config_path))

    log_directory = Path(os.environ.get("PERSONA_LOG_DIR", "logs/nonebot"))
    if not log_directory.is_absolute():
        log_directory = ROOT / log_directory
    log_directory.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(
        log_directory / "nonebot.log", rotation="10 MB", retention="14 days", compression="gz",
        level=0, filter=default_filter, format=default_format, diagnose=False, enqueue=True,
    )

    # A tuple prevents NoneBot from adding a working-directory .env to the file list.
    nonebot.init(_env_file=(connection_env,))
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)
    nonebot.load_from_toml(str(SOURCE / "pyproject.toml"))
    nonebot.run()


if __name__ == "__main__":
    main()
