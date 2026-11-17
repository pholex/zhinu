# 用户批准的完整服务地址只从检查输入中移除；同一行的其余内容仍须检查。
# 仅接受独立地址或引号、空白、尖括号包围的地址，路径和查询参数均不扩展。
BEGIN {
    approved = "https://api.githubcopilot.com/mcp/"
}
{
    rest = $0
    result = ""
    while ((pos = index(rest, approved)) > 0) {
        before = substr(rest, 1, pos - 1)
        after = substr(rest, pos + length(approved))
        left = pos == 1 || substr(before, length(before), 1) ~ /[[:space:]"'`<>]/
        right = after == "" || substr(after, 1, 1) ~ /[[:space:]"'`<>]/
        result = result before
        if (!left || !right) result = result approved
        rest = after
    }
    print result rest
}
