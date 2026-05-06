import asyncpg


async def open_pool(
    dsn: str,
    *,
    min_size: int = 2,
    max_size: int = 20,
    command_timeout: float = 30.0,
) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        command_timeout=command_timeout,
    )
