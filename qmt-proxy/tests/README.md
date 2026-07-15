# QMT Proxy 测试框架

QMT Proxy 项目的完整测试框架，包含 REST API 和 gRPC 接口的全面测试。

## 📁 目录结构

```
tests/
├── __init__.py                      # 测试模块初始化
├── conftest.py                      # 全局共享 fixtures
├── pytest.ini                       # pytest 全局配置
├── README.md                        # 本文件
│
├── rest/                            # REST API 测试目录
│   ├── __init__.py
│   ├── conftest.py                  # REST 测试共享 fixtures
│   ├── config.py                    # REST 测试配置
│   ├── client.py                    # REST 客户端封装
│   ├── README.md                    # REST 测试说明
│   ├── test_health_api.py          # 健康检查接口测试
│   ├── test_data_api.py            # 数据服务接口测试
│   └── test_trading_api.py         # 交易服务接口测试
│
└── grpc/                            # gRPC 测试目录
    ├── __init__.py
    ├── conftest.py                  # gRPC 测试共享 fixtures
    ├── config.py                    # gRPC 测试配置
    ├── client.py                    # gRPC 客户端封装
    ├── README.md                    # gRPC 测试说明
    ├── test_data_grpc_service.py   # 数据服务测试
    └── test_trading_grpc_service.py # 交易服务测试
```

## 🚀 快速开始

### 1. 安装依赖

```powershell
# 安装测试依赖
pip install pytest pytest-asyncio httpx grpcio grpcio-tools protobuf

# 或者使用 requirements.txt
pip install -r requirements.txt
```

### 2. 生成 protobuf 代码（仅 gRPC 测试需要）

```powershell
python scripts/generate_proto.py
```

### 3. 启动服务

```powershell
# 启动 REST API 服务
python run.py

# 或启动 gRPC 服务
python run_grpc.py

# 或启动混合模式（REST + gRPC）
python run_hybrid.py
```

### 4. 运行测试

```powershell
# 运行所有测试
pytest tests/ -v

# 只运行 REST 测试
pytest tests/rest/ -v

# 只运行 gRPC 测试
pytest tests/grpc/ -v
```

## 📋 测试命令参考

### 基本命令

```powershell
# 运行所有测试
pytest tests/ -v

# 显示详细输出（包括 print）
pytest tests/ -v -s

# 显示跳过的测试
pytest tests/ -v -rs

# 在第一个失败时停止
pytest tests/ -v -x

# 显示最慢的 10 个测试
pytest tests/ -v --durations=10
```

### 按标记运行

```powershell
# 只运行 REST 测试
pytest tests/ -v -m rest

# 只运行 gRPC 测试
pytest tests/ -v -m grpc

# 只运行集成测试
pytest tests/ -v -m integration

# 只运行性能测试
pytest tests/ -v -m performance

# 跳过慢速测试
pytest tests/ -v -m "not slow"

# 跳过集成测试
pytest tests/ -v -m "not integration"
```

### 按路径运行

```powershell
# 运行特定测试文件
pytest tests/rest/test_health_api.py -v

# 运行特定测试类
pytest tests/rest/test_data_api.py::TestDataAPI -v

# 运行特定测试方法
pytest tests/rest/test_data_api.py::TestDataAPI::test_get_market_data -v
```

### 按关键字运行

```powershell
# 运行包含 "health" 的测试
pytest tests/ -v -k "health"

# 运行包含 "market" 或 "sector" 的测试
pytest tests/ -v -k "market or sector"

# 运行不包含 "slow" 的测试
pytest tests/ -v -k "not slow"
```

## 📊 测试报告

### HTML 报告

```powershell
# 安装插件
pip install pytest-html

# 生成 HTML 报告
pytest tests/ -v --html=report.html --self-contained-html
```

### 覆盖率报告

```powershell
# 安装插件
pip install pytest-cov

# 生成覆盖率报告
pytest tests/ --cov=app --cov-report=html

# 查看报告
# 打开 htmlcov/index.html
```

### JUnit XML 报告（CI/CD）

```powershell
pytest tests/ -v --junitxml=junit.xml
```

## 🔧 配置说明

### 全局配置 (tests/pytest.ini)

```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
addopts = -v --strict-markers --tb=short
```

### REST API 配置 (tests/rest/config.py)

```python
BASE_URL = "http://localhost:8000"
API_KEY = "your-api-key"
SKIP_INTEGRATION_TESTS = True
```

### gRPC 配置 (tests/grpc/config.py)

```python
GRPC_SERVER_HOST = "localhost"
GRPC_SERVER_PORT = 50051
SKIP_INTEGRATION_TESTS = True
```

## 🎯 测试覆盖范围

### REST API 测试

| 类别 | 端点数 | 状态 |
|------|--------|------|
| 健康检查 | 5 | ✅ 完成 |
| 数据服务 | 7 | ✅ 完成 |
| 交易服务 | 11 | ✅ 完成 |
| **总计** | **23** | **✅ 完成** |

### gRPC 测试

| 类别 | 接口数 | 状态 |
|------|--------|------|
| 健康检查 | 2 | ✅ 完成 |
| 数据服务 | 9 | ✅ 完成 |
| 交易服务 | 7 | ✅ 完成 |
| **总计** | **18** | **✅ 完成** |

## 📝 编写新测试

### REST API 测试

```python
# tests/rest/test_example.py
import pytest
from tests.rest.client import RESTTestClient

class TestExampleAPI:
    """示例 API 测试"""
    
    @pytest.fixture
    def client(self, base_url: str, api_key: str):
        """创建测试客户端"""
        with RESTTestClient(base_url=base_url, api_key=api_key) as client:
            yield client
    
    def test_example(self, client: RESTTestClient):
        """测试示例端点"""
        response = client.client.get("/api/v1/example")
        result = client.assert_success(response)
        assert "data" in result
```

### gRPC 测试

```python
# tests/grpc/test_example.py
import pytest
from tests.grpc.client import GRPCTestClient

class TestExampleGrpc:
    """示例 gRPC 测试"""
    
    @pytest.fixture
    def client(self):
        """创建测试客户端"""
        with GRPCTestClient(host='localhost', port=50051) as client:
            yield client
    
    def test_example(self, client: GRPCTestClient):
        """测试示例接口"""
        response = client.some_method()
        client.assert_success(response)
```

## 🔍 常见问题

### Q1: 测试失败提示连接超时

**A:** 确保服务已启动：
```powershell
# REST API
python run.py

# gRPC
python run_grpc.py

# 混合模式
python run_hybrid.py
```

### Q2: 所有测试都被跳过

**A:** 检查配置文件中的 `SKIP_INTEGRATION_TESTS` 设置：
- `tests/rest/config.py`
- `tests/grpc/config.py`

将其设置为 `False` 以运行真实测试。

### Q3: gRPC 测试失败提示找不到模块

**A:** 生成 protobuf 代码：
```powershell
python scripts/generate_proto.py
```

### Q4: 导入错误

**A:** 确保项目根目录在 Python 路径中，或在项目根目录下运行测试。

## 🏗️ CI/CD 集成

### GitHub Actions 示例

```yaml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    
    steps:
    - uses: actions/checkout@v2
    
    - name: Set up Python
      uses: actions/setup-python@v2
      with:
        python-version: '3.10'
    
    - name: Install dependencies
      run: |
        pip install -r requirements.txt
        pip install pytest pytest-cov
    
    - name: Generate protobuf
      run: python scripts/generate_proto.py
    
    - name: Run tests
      run: pytest tests/ -v --cov=app --junitxml=junit.xml
    
    - name: Upload coverage
      uses: codecov/codecov-action@v2
```

## 📚 相关文档

- [REST API 测试文档](rest/README.md)
- [gRPC 测试文档](grpc/README.md)
- [pytest 官方文档](https://docs.pytest.org/)
- [httpx 文档](https://www.python-httpx.org/)
- [gRPC Python 文档](https://grpc.io/docs/languages/python/)

## 🤝 贡献指南

1. 为新功能编写测试
2. 确保所有测试通过
3. 更新相关文档
4. 提交 Pull Request

## 📄 许可证

本测试框架遵循项目主许可证。

---

**最后更新**: 2025-10-25  
**维护者**: Development Team
