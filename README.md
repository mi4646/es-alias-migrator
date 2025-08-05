
# Elasticsearch 索引迁移与别名管理工具

这是一个用于 **Elasticsearch 索引管理** 的 Python 脚本工具，支持基于 **索引别名 (alias)** 实现的自动化迁移、重命名、创建映射、异步 Reindex、任务追踪、alias 更新和旧索引清理。兼容 Elasticsearch 7.10+，支持 7.x 和 8.x 认证方式切换。

---

## ✅ 功能特性

- 自动判断当前 alias 是否指向索引或索引本体
- 支持自动/手动指定旧索引和新索引
- 支持异步 reindex，自动配置分片数、超时和轮询间隔
- 自动创建新索引并应用映射（支持文件或直接传 JSON）
- 自动更新 alias 到新索引
- 可选删除旧索引
- 支持 Elasticsearch 7.x / 8.x 客户端兼容（basic_auth / http_auth）
- 日志详细，含行号、时间戳、级别、文件名

---

## 📦 安装依赖
```bash
pip install -r requirements.txt
```
or
```bash
pip install urllib3 elasticsearch packaging 
```
---

## 🚀 使用示例

```bash
# 示例 1: 使用映射文件完整调用
python3 migrate_index.py \
  --old_index rc_android_attack \
  --new_index rc_android_attack_$(date +%s) \
  --alias rc_android_attack \
  --mapping_file_path mapping.json

# 示例 2: 仅提供 alias，自动查找最新索引并执行迁移
python3 migrate_index.py --alias rc_android_attack

# 示例 3: 通过 JSON 字符串传入映射内容
python3 migrate_index.py \
  --alias rc_android_attack \
  --mapping '{"properties": {"name": {"type": "text"}}}'
```

---

## 🧠 参数说明

| 参数 | 类型 | 说明 |
|------|------|------|
| `-a`, `--alias` | str | **[必须]** 目标索引别名 |
| `-o`, `--old_index` | str | 源索引名称（可省略，自动查找最新别名指向） |
| `-n`, `--new_index` | str | 目标新索引名称（可省略，默认加上时间戳） |
| `-m`, `--mapping_file_path` | str | 自定义映射文件路径 |
| `-M`, `--mapping` | str | 直接提供 JSON 格式映射字符串 |
| `-d`, `--delete_old` | flag | 是否删除旧索引（可选） |
| `-s`, `--suffix` | str | 重命名旧索引的后缀，默认 `_backup` |
| `-v`, `--version` | flag | 查看工具版本 |
| `-h`, `--help` | flag | 查看帮助文档 |

---

## 📁 默认路径和环境变量

- 默认映射文件：`./mapping.json`
- 支持通过环境变量配置连接信息：

| 环境变量 | 默认值 |
|----------|--------|
| `AGREEMENT` | `http` |
| `HOST` | `192.168.1.183` |
| `PORT` | `9203` |
| `USERNAME` | `elastic` |
| `PASSWORD` | `admin@123` |
| `TIMEOUT` | `5` |
| `MAX_RETRIES` | `5` |
| `VERIFY_CERTS` | `True` |
| `RETRY_ON_TIMEOUT` | `True` |

---

## 🧪 兼容性

- ✅ Elasticsearch 7.x & 8.x
- ✅ 适配 `http_auth` 与 `basic_auth`
- ✅ 支持异步 Reindex + 子任务进度追踪

---

## 📄 License

MIT License
