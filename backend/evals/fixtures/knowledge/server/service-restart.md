# 服务重启标准流程

1. 先摘除负载均衡后端。
2. `systemctl restart <service>`。
3. 确认健康检查通过后再加回负载均衡。
