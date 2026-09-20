# 示例请求

启动服务后运行端到端演示脚本（健康检查 → 建样本 → 乱序读数 → 状态/循环 →
幂等去重 → 迟到数据 → 告警）：

```bash
./examples/example_requests.sh
# 或指定地址
BASE_URL=http://127.0.0.1:8000 ./examples/example_requests.sh
```

也可以用目录中的 JSON 载荷手工请求：

```bash
curl -X POST http://localhost:8000/samples \
  -H 'Content-Type: application/json' \
  -d @examples/create-sample.json

curl -X POST http://localhost:8000/samples/SAMPLE-001/readings \
  -H 'Content-Type: application/json' \
  -d @examples/readings.json
```

启动后可直接打开交互式文档：<http://localhost:8000/docs>
