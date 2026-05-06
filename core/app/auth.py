from fastapi import HTTPException, Request


async def auth_stub(request: Request) -> str:
    """Day-2 auth stub. Accepts any `Authorization: Bearer ng_*` token; agent_id = "anonymous".

    Day 4 replaces this with argon2id verify against `nautgate.api_keys`, with a cached
    `keyId → agent_id` map after first verify (Tech Paper §7.1, §7.2).
    """
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer ng_"):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    return "anonymous"
