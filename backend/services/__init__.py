# services 包：放业务逻辑（可以类比 C++ 项目里的 service 层）
#
# 为什么要和 routers 分开？
#   routers 只管"接请求、返回响应"，
#   services 负责真正干活（调大模型、合成音乐、读写库）。
#   这样以后你想把音乐生成换成别家 API，只需要改 services，接口层一行都不用动。
