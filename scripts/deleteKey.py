deleteKeyLuaScript = """
        local current = redis.call("get",KEYS[1])
        if current == ARGV[1] then
            return redis.call("del",KEYS[1])
        else
            return 0
        end
"""
