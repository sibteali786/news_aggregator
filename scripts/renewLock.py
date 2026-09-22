renewLockLuaScript = """
    local current = redis.call("get", KEYS[1])
    if current == ARGV[1] then
        return redis.call("expire", KEYS[1], ARGV[2])
    else
        return 0
    end
"""
