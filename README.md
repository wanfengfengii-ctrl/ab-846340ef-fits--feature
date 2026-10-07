# fits-audit — FITS 归档前审计服务

天文数据中心在归档观测文件前，用本服务逐个核对 FITS 文件的头部、数据边界与标准校验字，
避免被截断或拼接的文件在读取软件容错后进入长期存储。

服务仅依赖 Python 3.11 标准库，无第三方运行时依赖。

## 快速开始

```bash
# 构建并启动 API（宿主机端口可用 API_PORT 配置，默认 8080）
API_PORT=8080 docker compose up --build api

# 运行一次性 verify 服务（代码测试 + 应用构建 + HTTP 冒烟），并取其退出码
docker compose up --build --exit-code-from verify --abort-on-container-exit verify
echo "verify exit code: $?"

# 或者：docker compose run --build --rm verify
```

`verify` 服务等待 `api` 健康检查后自行执行三个阶段并退出，退出码按位汇总：

| 位 | 值 | 阶段 |
|----|----|------|
| 0 | 1 | 代码测试（`python -m unittest` 全量单元测试） |
| 1 | 2 | 应用构建（`compileall` 字节编译 + 应用模块导入） |
| 2 | 4 | HTTP 冒烟（合法文件、DATASUM/CHECKSUM 摘要损坏、截断文件、尾随字节、错误媒体类型） |

全部通过时退出码为 0。

## API

### `POST /api/fits/audit`

* 请求体：原始 FITS 文件，`Content-Type: application/fits`，不超过 16 MiB（16 × 1024 × 1024 字节）。
* 审计完成一律返回 **HTTP 200**，归档裁决在 JSON 的 `conclusion` 字段（`ACCEPTED` / `REJECTED`）。
* 请求级错误返回相应 4xx：`415` 媒体类型不符、`411` 缺少 Content-Length、`413` 超过大小上限、
  `404` 路径不存在、`405` 方法不允许（`GET /api/fits/audit`）。

响应示例（合法文件）：

```json
{
  "conclusion": "ACCEPTED",
  "fileSize": 8640,
  "hduCount": 2,
  "limits": {"maxFileBytes": 16777216, "maxHdus": 16},
  "hdus": [
    {
      "index": 0,
      "type": "PRIMARY",
      "range": {"start": 0, "end": 2880},
      "header": {"offset": 0, "bytes": 2880, "cards": 7},
      "data": {"offset": 2880, "bytes": 0, "paddedBytes": 0},
      "datasum": {"verdict": "VALID", "stored": 0, "computed": 0},
      "checksum": {"verdict": "VALID", "stored": "B8dBC7ZBB7dBB7ZB", "computed": "B8dBC7ZBB7dBB7ZB"}
    },
    {
      "index": 1,
      "type": "IMAGE",
      "range": {"start": 2880, "end": 8640},
      "header": {"offset": 2880, "bytes": 2880, "cards": 10},
      "data": {"offset": 5760, "bytes": 120, "paddedBytes": 2880},
      "datasum": {"verdict": "VALID", "stored": 202968300, "computed": 202968300},
      "checksum": {"verdict": "VALID", "stored": "LfDeNfCbLfCbLfCb", "computed": "LfDeNfCbLfCbLfCb"}
    }
  ],
  "failure": null
}
```

每个 HDU 按顺序报告：零基编号 `index`、类型 `type`（`PRIMARY` / `IMAGE`）、半开字节区间
`range`（`start` 含、`end` 不含）、头部与数据段位置、数据字节数 `data.bytes`（未补齐的声明长度），
以及 `datasum` / `checksum` 裁决（`VALID` / `ABSENT`；`INVALID` 会导致整份文件被拒绝，
体现在 `failure` 中）。

拒绝示例（截断文件）：

```json
{
  "conclusion": "REJECTED",
  "fileSize": 6000,
  "hduCount": 1,
  "hdus": [ /* 已通过审计的 HDU 0 */ ],
  "failure": {
    "hdu": 1,
    "reason": "TRUNCATED_DATA",
    "offset": 5760,
    "message": "data section of HDU 1 needs 120 bytes (plus 2760 bytes of padding) at offset 5760, but only 240 bytes remain in the file",
    "details": {"dataOffset": 5760, "declaredDataBytes": 120, "requiredEndOffset": 8640, "fileSize": 6000}
  }
}
```

审计按 HDU 顺序进行，在**最早失败**处停止：`failure` 稳定给出失败 HDU 的零基编号、
原因码 `reason` 与可定位的字节偏移 `offset`（同一输入字节串永远产生同一报告）。

### 其他端点

* `GET /health` — 健康检查（Docker healthcheck 使用），返回 `{"status": "ok"}`。
* `GET /` — 服务元信息。

## 审计规则

文件结构：一个主 HDU（首卡 `SIMPLE = T`）+ 至多 15 个 IMAGE 扩展（首卡 `XTENSION= 'IMAGE   '`），
共至多 16 个 HDU。

头部：每张卡 80 字节、可打印 ASCII（`0x20`–`0x7E`）；关键字左对齐、大写字符集；
以 `END` 卡结束；头部用空格补齐到 2880 字节的整数倍（补齐区必须全部为空格）。

数据段长度由 `BITPIX`、`NAXIS`、各 `NAXISn`、`PCOUNT`、`GCOUNT` 一致确定：

```
dataBytes = |BITPIX|/8 × GCOUNT × (PCOUNT + NAXIS1 × NAXIS2 × … × NAXISn)   (NAXIS=0 时为 0)
```

数据段按 2880 字节补齐，**补齐区不得藏有非零字节**。数据段（含补齐）越界即判截断。
最后一个 HDU 之后不允许任何尾随字节。

校验字（遵循 FITS checksum 约定，与 CFITSIO / astropy 双向验证一致）：

* `DATASUM`：数据段的 32 位 1 的补码和（大端字），支持整数卡与 astropy 的字符串卡两种写法；
* `CHECKSUM`：将卡内 16 字符值域置为 ASCII `'0'` 后，对整个头部求和并以数据段 DATASUM 为种子，
  取补后按标准算法（含循环移位）编码为 16 字符，与卡值逐字符比较；
* 两卡缺失时裁决为 `ABSENT`，不导致拒绝；存在但不符则拒绝整份文件。

### 原因码

| 原因码 | 含义 | offset 指向 |
|--------|------|--------------|
| `EMPTY_FILE` | 空文件 | 0 |
| `FILE_TOO_LARGE` | 超过 16 MiB 审计上限 | 上限位置 |
| `TRUNCATED_HEADER` | 头部在 END 卡前被截断 | 该 HDU 头部起点 |
| `INVALID_CARD` | 非 ASCII 字节、畸形关键字/值指示符等 | 出错字节/卡 |
| `NONSPACE_HEADER_PADDING` | 头部补齐区含非空格字节 | 首个违例字节 |
| `MISSING_KEYWORD` | 缺少必需关键字（SIMPLE/XTENSION/BITPIX/NAXIS/NAXISn/PCOUNT/GCOUNT） | END 卡或首卡 |
| `DUPLICATE_KEYWORD` | 结构性关键字重复 | 第二次出现的卡 |
| `INVALID_KEYWORD_VALUE` | 关键字值非法（BITPIX 不在枚举内、NAXIS 越界、GCOUNT<1 等） | 该卡 |
| `KEYWORD_CONFLICT` | NAXISn 与 NAXIS 冲突 | 冲突的卡 |
| `UNSUPPORTED_EXTENSION` | 非 IMAGE 扩展（如 BINTABLE） | 该 HDU 首卡 |
| `UNSUPPORTED_FEATURE` | 随机群（GROUPS = T） | GROUPS 卡 |
| `TRUNCATED_DATA` | 数据段（含补齐）越界 | 数据段起点 |
| `NONZERO_DATA_PADDING` | 数据补齐区藏有非零字节 | 首个违例字节 |
| `DATASUM_MISMATCH` | DATASUM 与计算值不符 | DATASUM 卡 |
| `CHECKSUM_MISMATCH` | CHECKSUM 与计算值不符 | CHECKSUM 卡 |
| `TOO_MANY_HDUS` | 超过 1 主 + 15 扩展 | 第 17 个 HDU 起点 |
| `TRAILING_BYTES` | 末尾不足一个 2880 字节块的残余字节 | 残余起点 |

## 本地开发（无 Docker）

```bash
python3 -m unittest discover -s tests -t .        # 单元测试
PORT=8000 python3 -m fitsaudit                    # 启动服务
API_URL=http://127.0.0.1:8000 python3 -m verify   # 运行 verify 三阶段
```

## 仓库结构

```
fitsaudit/
  core.py       解析、结构校验、DATASUM/CHECKSUM 验证（纯标准库）
  server.py     HTTP API（http.server，线程模式）
  fixtures.py   测试/冒烟用 FITS 构造器（复用核心校验和函数）
tests/
  test_core.py / test_server.py
  data/valid_astropy.fits   astropy 生成的基准文件（已知答案测试）
verify/
  __main__.py   一次性 verify 服务（测试 + 构建 + 冒烟，退出码按位汇总）
Dockerfile / docker-compose.yml
```
