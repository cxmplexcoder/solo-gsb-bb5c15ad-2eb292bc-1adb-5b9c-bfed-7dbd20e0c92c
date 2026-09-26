# 访谈字幕时间轴校准 API

基于 Python + FastAPI 的字幕时间轴校准服务，提供四个接口：

1. **`POST /anchors/suggest` 锚点推荐**：提交**原片**与**成片**两条按开始时间严格递增的字幕序列，服务端在允许跳过片段的前提下做**全局最优**的文字精确匹配（重复台词、删段均按全局最优处理，而非逐条贪心），返回可直接用于 `/calibrate` 的同步锚点。
2. **`POST /calibrate` 时间轴校准**：提交以**原录音毫秒**计时的字幕片段，以及至少两组「原时间 → 成片时间」同步锚点；服务端在相邻锚点之间做**分段线性换算**，输出成片时间轴上的字幕起止时间。
3. **`POST /layout/wrap` 字幕断行**：提交字幕片段与行宽、行数约束，服务端按 **Unicode 码点显示宽度**（东亚全角/宽字符占 2 格、其余占 1 格）为每段做**全局最优**断行，保留原时码与文字，返回每段的各行文字与断行字符偏移。
4. **`POST /edits/project` 剪辑投影**：提交源字幕 `segments` 与按成片顺序排列的 `clips`（允许源区间**重叠、重排、重复使用**）；服务端按**半开区间**求每条字幕与每段剪辑的交集，再按剪辑时长与源区间长度之比**线性映射**到成片时间轴，四舍五入到毫秒后丢弃零时长片段，返回可直接送入 `/layout/wrap` 的片段及未产生输出的字幕下标。

## 功能说明

### 锚点推荐 `/anchors/suggest`

- 接收 `original`（原片）与 `final`（成片）两条字幕序列，每条字幕含整数毫秒 `start` 与 `text`，两列 `start` 均须**非负且严格递增**。
- 匹配前对文字做规范化：**去首尾空白，并把连续空白（含制表符、全角空格等 Unicode 空白）统一为单个空格**，之后做**精确相等**匹配（不做模糊/包含匹配）。
- 在允许两侧任意跳过片段的前提下，选择时间顺序一致的一对一匹配（即 LCS 型有序匹配），并按以下优先级取**全局最优**：
  1. 匹配数最多；
  2. 各配对序号差绝对值之和 `sum(|i - j|)` 最小；
  3. 仍有平局时，按配对序号序列 `[(i, j), ...]` 的字典序裁决。

  因此**重复台词**（同一句出现多次）与**删段**（成片剪掉若干片段）都能得到全局最优结果，不会因为逐条贪心而错配。
- 返回每个匹配片段的两个 `start` 组成的锚点 `{original, final}`，并附来源下标 `original_index` / `final_index`；返回的 `original` / `final` 两列天然严格递增，可**直接作为 `/calibrate` 的 `anchors`**。
- 以下情况返回 `422`，错误体中带**轨道名**（`original` / `final`）与**下标**：
  - 任一 `text` 去空白后为空（`empty_text`）；
  - 任一 `start` 为负（`negative_time`）；
  - 任一轨道的 `start` 非严格递增（`not_strictly_increasing`）；
  - 全部校验通过但匹配少于 2 对（`not_enough_matches`），因为至少两对才能构成校准锚点。

### 时间轴校准 `/calibrate`

- 接收至少 **2 组**同步锚点 `(original, final)`，服务端校验：
  - `original` 列必须**严格递增**；
  - `final` 列必须**严格递增**。
- 每条字幕片段的 `start`、`end` 各自落在锚点覆盖的区间内，按所在线段独立线性换算：

  ```
  final(t) = final_i + (final_{i+1} - final_i) / (original_{i+1} - original_i) * (t - original_i)
  ```

  跨越一个或多个锚点的片段，其两个端点分别按各自所在线段换算。
- 换算结果**四舍五入到整数毫秒**（0.5 进位），并按输入顺序返回原字幕文字。
- 以下情况返回 `422` 错误，错误体中包含出错片段/锚点的**位置下标**：
  - 锚点少于 2 组；
  - 任一列锚点非严格递增；
  - 片段端点超出锚点覆盖范围（早于首个锚点或晚于末个锚点）；
  - 原片段时长非正（`end <= start`）；
  - 换算后/四舍五入后片段时长非正。

### 字幕断行 `/layout/wrap`

- 接收 `segments`（每段含整数毫秒 `start` / `end` 与 `text`）、正整数 `max_width`（每行最大显示宽度，单位：格）与 `max_lines`（每段最多行数，**1～4**）。
- **宽度按 Unicode 码点计算**：东亚全角/宽字符（`East_Asian_Width` 为 `F` 或 `W`，如汉字、全角标点、多数 emoji）占 **2 格**，其余码点占 **1 格**。
- 断行规则：
  - 只在**字符（码点）之间**断行，不拆分字符；
  - 每行**非空**且宽度不超过 `max_width`；
  - **避头尾**：行首不得为 `，。！？；：）】` 中的字符，行尾不得为 `（【`（因此首字符为避头标点、末字符为开括号、或单个字符即超宽的片段都无法排版）。
- 对每段，在所有满足约束的方案中依次选择（**全局最优，而非逐行贪心**）：
  1. 行数最少；
  2. 最大行宽最小；
  3. 各行宽平方和最小（行宽更均衡）；
  4. 断点序列字典序最小。
- 成功时按输入顺序返回每段的 `index`、原 `start` / `end` / `text`、断行后的 `lines` 与 `breaks`（断行字符偏移：`breaks` 中的 `i` 表示在原文第 `i` 个码点之前断行，从 0 计，升序；单行时为空数组）。
- 以下情况返回 `422`，错误体中带出错片段**下标**（所有错误一次性收集返回）：
  - 任一片段时长非正（`end <= start`，`non_positive_duration`）；
  - 任一片段在约束下无法排版（`cannot_wrap`）：文字为空、最少行数超过 `max_lines`、单字超宽、或避头尾规则导致无可行断点。
- `max_width` 非正整数、`max_lines` 超出 1～4、`segments` 为空或缺字段等请求格式错误，返回 `422` 且 `error` 为 `invalid_request`。

### 剪辑投影 `/edits/project`

- 接收：
  - `segments[]`：源字幕，每条含整数毫秒 `start` / `end`（半开区间 `[start, end)`）与 `text`，要求 `start >= 0` 且 `end > start`；
  - `clips[]`：**按成片顺序排列**的剪辑段，每段含整数毫秒 `source_start` / `source_end`（源时间轴上的半开区间，要求 `source_start >= 0` 且 `source_end > source_start`）与正整数 `duration`（该段在成片中占用的毫秒数）。
- 各段剪辑**允许源区间重叠、重排与重复使用**：第 `j` 段剪辑（下标即成片顺序）从前面各段 `duration` 的**累计和** `cut_start_j` 处开始，占用成片轴半开区间 `[cut_start_j, cut_start_j + duration_j)`。
- 对每条字幕与每段剪辑，按半开区间求源时间交集 `[lo, hi) = [start,end) ∩ [source_start,source_end)`；**端点相接不算相交**（`lo >= hi` 时跳过）。交集两端按剪辑速率线性映射到成片轴（精确分数计算）：

  ```
  final(t) = cut_start_j + duration_j / (source_end_j - source_start_j) * (t - source_start_j)
  ```

  因此 `duration` 与源区间长度之比即为成片相对源片的速率比：拉伸（duration 更长）或压缩（duration 更短）都由该比值决定；源区间如何重排、复用则完全由 `clips` 的成片顺序决定。
- 两个端点分别**四舍五入到整数毫秒（0.5 进位）**；舍入后 `end <= start`（零时长或负时长）的片段**直接丢弃**。
- 成功响应：
  - `segments[]`：每个投影片段含成片轴整数毫秒 `start` / `end`、原 `text`（原样保留）、来源字幕下标 `segment_index` 与来源剪辑下标 `clip_index`（均从 0 开始）。除两个下标字段外，元素结构与 `/layout/wrap` 的 `segments` 输入一致——把本响应的 `segments` 连同 `max_width` / `max_lines` 直接 `POST` 给 `/layout/wrap` 即可（多余的下标字段会被忽略）。
  - 片段按 `(start, end, segment_index, clip_index)` **升序**排列。
  - `unused_segment_indices[]`：**未在任何剪辑中产生有效输出**的源字幕下标（升序）；没有交集、或全部交集都在舍入后被丢弃的字幕都会出现在这里。
- 以下情况返回 `422`，错误一次性收集，`location` 标明字段、`index` 标明元素下标：
  - `segments` 或 `clips` 为空列表（`empty`，`index` 为 `null`）；
  - `segments[].start` 或 `clips[].source_start` 为负（`negative_time`）；
  - 字幕 `end <= start`、剪辑 `source_end <= source_start`、或剪辑 `duration <= 0`（`non_positive_duration`）。

  缺字段、类型错误等请求格式问题返回 `422` 且 `error` 为 `invalid_request`。

## 单条启动命令

无需手动安装依赖，依赖安装与服务启动均已纳入容器启动流程（构建镜像时安装依赖，容器启动时启动服务）：

```bash
docker compose up --build -d
```

停止服务：

```bash
docker compose down
```

> 也可以不使用 Compose：
>
> ```bash
> docker build -t subtitle-calibrator .
> docker run -d -p 8000:8000 subtitle-calibrator
> ```

## 必要配置

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| 端口 | 容器内服务端口由环境变量 `PORT` 控制（见 `docker-compose.yml`）；宿主机映射端口为 `${PORT:-8000}`，可在启动前 `export PORT=9000` 修改 | `8000` |

服务无其他外部依赖（无需数据库）。

## API 地址和端口

服务启动后监听 `http://localhost:8000`：

| 用途 | 地址 |
| --- | --- |
| 健康检查 | `GET http://localhost:8000/health` |
| 锚点推荐接口 | `POST http://localhost:8000/anchors/suggest` |
| 校准接口 | `POST http://localhost:8000/calibrate` |
| 字幕断行接口 | `POST http://localhost:8000/layout/wrap` |
| 剪辑投影接口 | `POST http://localhost:8000/edits/project` |
| 交互式文档（Swagger UI） | `http://localhost:8000/docs` |
| OpenAPI Schema | `http://localhost:8000/openapi.json` |

## 接口约定

### 请求 `POST /anchors/suggest`

```json
{
  "original": [
    { "start": 0, "text": "大家好" },
    { "start": 1200, "text": "这一段在成片里被剪掉了" },
    { "start": 2600, "text": "欢迎来到访谈节目" },
    { "start": 4000, "text": "我们开始今天的话题" }
  ],
  "final": [
    { "start": 500, "text": "大家好" },
    { "start": 1500, "text": "  欢迎来到  访谈节目 " },
    { "start": 3000, "text": "我们开始今天的话题" }
  ]
}
```

- `original[]` / `final[]`：两条字幕序列；`start` 为整数毫秒，各自须**非负且严格递增**；`text` 为必填字符串。
- 匹配前每条 `text` 都会去首尾空白并把连续空白压成单个空格（如上例成片第 2 条规范化后为 `欢迎来到 访谈节目`，与原片文字完全一致才能匹配）。

### 成功响应 `200`

```json
{
  "anchors": [
    { "original": 0, "final": 500, "original_index": 0, "final_index": 0 },
    { "original": 2600, "final": 1500, "original_index": 2, "final_index": 1 },
    { "original": 4000, "final": 3000, "original_index": 3, "final_index": 2 }
  ]
}
```

`anchors[]` 按顺序排列，两列时间严格递增；把其中的 `original` / `final` 取出即可原样传给 `/calibrate`。`original_index` / `final_index` 为匹配片段在两条输入序列中的下标（从 0 开始），便于回溯。

### 锚点推荐错误响应 `422`

所有错误一次性收集返回，`location` 标明轨道与字段，`index` 为出错字幕下标（从 0 开始）：

```json
{
  "error": "anchor_suggest_failed",
  "detail": [
    {
      "location": "original.start",
      "index": 3,
      "code": "not_strictly_increasing",
      "message": "original start 未严格递增：第 3 条为 2600，前一条为 2600"
    },
    {
      "location": "final.text",
      "index": 1,
      "code": "empty_text",
      "message": "final 第 1 条 text 去空白后为空"
    }
  ]
}
```

错误代码：

| code | 含义 |
| --- | --- |
| `empty_text` | 字幕文字去首尾空白并规范化后为空 |
| `negative_time` | `start` 为负毫秒 |
| `not_strictly_increasing` | 同轨道 `start` 未严格递增 |
| `not_enough_matches` | 校验通过但全局匹配少于 2 对，无法构成锚点 |

请求体本身格式错误（缺字段、类型错误等）返回 `422` 且 `error` 为 `invalid_request`。

### 请求 `POST /calibrate`

```json
{
  "anchors": [
    { "original": 0, "final": 1000 },
    { "original": 2000, "final": 4000 }
  ],
  "segments": [
    { "start": 0, "end": 500, "text": "大家好" },
    { "start": 500, "end": 2000, "text": "欢迎来到访谈节目" }
  ]
}
```

- `anchors[].original` / `anchors[].final`：锚点的原时间与成片时间，整数毫秒，两列均须严格递增。
- `segments[].start` / `segments[].end`：片段在原录音上的起止时间，整数毫秒，须落在锚点覆盖范围内。
- `segments[].text`：字幕文字，原样返回。

### 成功响应 `200`

```json
{
  "segments": [
    { "index": 0, "start": 1000, "end": 1500, "text": "大家好" },
    { "index": 1, "start": 1500, "end": 4000, "text": "欢迎来到访谈节目" }
  ]
}
```

### 错误响应 `422`

所有片段错误会一次性收集返回，`index` 为片段在输入中的位置（从 0 开始）：

```json
{
  "error": "calibration_failed",
  "detail": [
    {
      "location": "segment",
      "index": 1,
      "code": "out_of_anchor_range",
      "message": "第 1 段 [2100, 2600] 超出锚点覆盖范围 [0, 2000]"
    }
  ]
}
```

错误代码：

| code | 含义 |
| --- | --- |
| `not_enough_anchors` | 锚点少于 2 组 |
| `not_strictly_increasing` | 锚点 original/final 列非严格递增 |
| `out_of_anchor_range` | 片段端点超出锚点覆盖范围 |
| `non_positive_duration` | 原片段 `end <= start` |
| `non_positive_mapped_duration` | 线性换算后时长非正 |
| `non_positive_mapped_duration_after_rounding` | 四舍五入到整数毫秒后时长非正（成片轴压缩过度） |

### 请求 `POST /layout/wrap`

```json
{
  "segments": [
    { "start": 0, "end": 2400, "text": "大家好，欢迎来到访谈节目" },
    { "start": 2400, "end": 4200, "text": "他说：你好" }
  ],
  "max_width": 8,
  "max_lines": 3
}
```

- `segments[].start` / `segments[].end`：片段起止时间，整数毫秒，要求 `end > start`；原样返回。
- `segments[].text`：字幕文字，原样返回；断行只发生在字符之间。
- `max_width`：每行最大显示宽度（格），正整数；全角/宽字符占 2 格，其余占 1 格。
- `max_lines`：每段最多行数，整数，1～4。

### 成功响应 `200`

```json
{
  "segments": [
    {
      "index": 0,
      "start": 0,
      "end": 2400,
      "text": "大家好，欢迎来到访谈节目",
      "lines": ["大家好，", "欢迎来到", "访谈节目"],
      "breaks": [4, 8]
    },
    {
      "index": 1,
      "start": 2400,
      "end": 4200,
      "text": "他说：你好",
      "lines": ["他说：", "你好"],
      "breaks": [3]
    }
  ]
}
```

`lines` 为断行后的各行文字（拼接起来与原 `text` 完全一致）；`breaks` 为断行字符偏移：`i` 表示在原文第 `i` 个码点（从 0 计）之前断行，升序排列，单行时为空数组。上例第 2 段中，`：` 是避头字符不会出现在行首，因此断在 `：` 之后（偏移 3）。

### 断行错误响应 `422`

所有片段错误一次性收集返回，`index` 为片段在输入中的位置（从 0 开始）：

```json
{
  "error": "layout_wrap_failed",
  "detail": [
    {
      "location": "segment",
      "index": 0,
      "code": "non_positive_duration",
      "message": "片段时长非正：start=1000，end=1000，要求 end > start"
    },
    {
      "location": "segment",
      "index": 1,
      "code": "cannot_wrap",
      "message": "第 1 段无法在 max_width=4、max_lines=2 的约束下完成断行排版"
    }
  ]
}
```

错误代码：

| code | 含义 |
| --- | --- |
| `non_positive_duration` | 片段 `end <= start`，时长非正 |
| `cannot_wrap` | 在 `max_width` / `max_lines` 约束下无法排版（文字为空、行数超限、单字超宽、避头尾规则导致无可行断点等） |

`max_width` 非正整数、`max_lines` 超出 1～4、`segments` 为空或字段缺失/类型错误时，返回 `422` 且 `error` 为 `invalid_request`。

### 请求 `POST /edits/project`

```json
{
  "segments": [
    { "start": 0, "end": 1000, "text": "片头被剪掉了" },
    { "start": 1000, "end": 2500, "text": "欢迎来到访谈节目" },
    { "start": 2500, "end": 4000, "text": "我们开始今天的话题" }
  ],
  "clips": [
    { "source_start": 2500, "source_end": 4000, "duration": 1500 },
    { "source_start": 1000, "source_end": 2500, "duration": 3000 }
  ]
}
```

- `segments[]`：源字幕；`start` / `end` 为源时间轴上的整数毫秒，半开区间，要求 `start >= 0`、`end > start`；`text` 原样保留（可省略，默认为空字符串）。
- `clips[]`：**数组顺序即成片顺序**。第 0 段从成片 `0ms` 开始，之后每段从累计 `duration` 处开始。每段：
  - `source_start` / `source_end`：取用的源半开区间，要求 `source_start >= 0`、`source_end > source_start`；不同剪辑段之间允许**重叠、任意重排、重复使用**同一源区间；
  - `duration`：该段在成片中占用的毫秒数，正整数；映射速率为 `duration / (source_end - source_start)`。
- 上例中源时间 `[2500,4000)` 被放到成片最前面，`[1000,2500)` 随后并以 2 倍长度拉伸；源区间 `[0,1000)` 未被任何剪辑使用。

### 成功响应 `200`

```json
{
  "segments": [
    {
      "start": 0,
      "end": 1500,
      "text": "我们开始今天的话题",
      "segment_index": 2,
      "clip_index": 0
    },
    {
      "start": 1500,
      "end": 4500,
      "text": "欢迎来到访谈节目",
      "segment_index": 1,
      "clip_index": 1
    }
  ],
  "unused_segment_indices": [0]
}
```

- `segments[]` 按 `(start, end, segment_index, clip_index)` 升序排列；`segment_index` / `clip_index` 为来源下标（从 0 开始）。
- 一条源字幕若与多段剪辑相交（源区间被重叠/重复使用时），会产生**多条**输出；只有端点相接（半开区间无实际交集）或交集舍入后零时长时不产生输出。
- 该数组可连同 `max_width` / `max_lines` 直接作为 `/layout/wrap` 的 `segments`，两个下标字段会被忽略。
- `unused_segment_indices[]` 为未产生任何有效输出的源字幕下标（上例第 0 条完全落在所用源区间之外）。

### 剪辑投影错误响应 `422`

所有校验错误一次性收集返回，`location` 标明字段，`index` 为元素下标（列表本身为空时为 `null`）：

```json
{
  "error": "edit_project_failed",
  "detail": [
    {
      "location": "segments",
      "index": null,
      "code": "empty",
      "message": "segments 不能为空"
    },
    {
      "location": "segments.start",
      "index": 0,
      "code": "negative_time",
      "message": "segments 第 0 条 start 为负：-5"
    },
    {
      "location": "segments.end",
      "index": 1,
      "code": "non_positive_duration",
      "message": "segments 第 1 条时长非正：start=100，end=100，要求 end > start"
    },
    {
      "location": "clips.source_start",
      "index": 0,
      "code": "negative_time",
      "message": "clips 第 0 段 source_start 为负：-1"
    },
    {
      "location": "clips.source_end",
      "index": 1,
      "code": "non_positive_duration",
      "message": "clips 第 1 段源区间长度非正：source_start=100，source_end=100，要求 source_end > source_start"
    },
    {
      "location": "clips.duration",
      "index": 2,
      "code": "non_positive_duration",
      "message": "clips 第 2 段 duration 非正：0，要求为正整数"
    }
  ]
}
```

错误代码：

| code | 含义 |
| --- | --- |
| `empty` | `segments` 或 `clips` 为空列表（`location` 指明是哪一个，`index` 为 `null`） |
| `negative_time` | `segments[].start` 或 `clips[].source_start` 为负毫秒 |
| `non_positive_duration` | 字幕 `end <= start`、剪辑 `source_end <= source_start`、或剪辑 `duration <= 0` |

字段缺失或类型错误等请求格式问题返回 `422` 且 `error` 为 `invalid_request`。

## 调用示例

### 第一步：`/anchors/suggest` 自动生成锚点

```bash
curl -s -X POST http://localhost:8000/anchors/suggest \
  -H "Content-Type: application/json" \
  -d '{
    "original": [
      {"start": 0, "text": "大家好"},
      {"start": 1200, "text": "剪掉的寒暄"},
      {"start": 2600, "text": "欢迎来到访谈节目"},
      {"start": 4000, "text": "我们开始今天的话题"}
    ],
    "final": [
      {"start": 500, "text": "大家好"},
      {"start": 1500, "text": "欢迎来到访谈节目"},
      {"start": 3000, "text": "我们开始今天的话题"}
    ]
  }'
```

响应：

```json
{
  "anchors": [
    { "original": 0, "final": 500, "original_index": 0, "final_index": 0 },
    { "original": 2600, "final": 1500, "original_index": 2, "final_index": 1 },
    { "original": 4000, "final": 3000, "original_index": 3, "final_index": 2 }
  ]
}
```

匹配少于 2 对（或输入不合法）时返回 `422`，例如两条序列只有一句相同：

```json
{
  "error": "anchor_suggest_failed",
  "detail": [
    {
      "location": "anchors",
      "index": null,
      "code": "not_enough_matches",
      "message": "匹配片段仅 1 对，至少需要 2 对才能构成同步锚点"
    }
  ]
}
```

### 第二步：`/calibrate` 用上一步的锚点换算字幕

把上一步返回的 `original` / `final` 原样作为 `anchors` 传入：

```bash
curl -s -X POST http://localhost:8000/calibrate \
  -H "Content-Type: application/json" \
  -d '{
    "anchors": [
      {"original": 0, "final": 500},
      {"original": 2600, "final": 1500},
      {"original": 4000, "final": 3000}
    ],
    "segments": [
      {"start": 900, "end": 2100, "text": "这是一段访谈字幕"}
    ]
  }'
```

响应：

```json
{
  "segments": [
    { "index": 0, "start": 846, "end": 1308, "text": "这是一段访谈字幕" }
  ]
}
```

### `/layout/wrap` 为字幕建议断行

```bash
curl -s -X POST http://localhost:8000/layout/wrap \
  -H "Content-Type: application/json" \
  -d '{
    "segments": [
      {"start": 0, "end": 2400, "text": "大家好，欢迎来到访谈节目"},
      {"start": 2400, "end": 4200, "text": "他说：你好"}
    ],
    "max_width": 8,
    "max_lines": 3
  }'
```

响应：

```json
{
  "segments": [
    {
      "index": 0,
      "start": 0,
      "end": 2400,
      "text": "大家好，欢迎来到访谈节目",
      "lines": ["大家好，", "欢迎来到", "访谈节目"],
      "breaks": [4, 8]
    },
    {
      "index": 1,
      "start": 2400,
      "end": 4200,
      "text": "他说：你好",
      "lines": ["他说：", "你好"],
      "breaks": [3]
    }
  ]
}
```

### `/edits/project` 把字幕投影到剪辑后的时间轴

剪辑段按数组顺序排列：先放源时间 `[2500,4000)`（时长不变），再放 `[1000,2500)` 并拉伸到 3000ms；源区间 `[0,1000)` 未被采用：

```bash
curl -s -X POST http://localhost:8000/edits/project \
  -H "Content-Type: application/json" \
  -d '{
    "segments": [
      {"start": 0, "end": 1000, "text": "片头被剪掉了"},
      {"start": 1000, "end": 2500, "text": "欢迎来到访谈节目"},
      {"start": 2500, "end": 4000, "text": "我们开始今天的话题"}
    ],
    "clips": [
      {"source_start": 2500, "source_end": 4000, "duration": 1500},
      {"source_start": 1000, "source_end": 2500, "duration": 3000}
    ]
  }'
```

响应：

```json
{
  "segments": [
    {"start": 0, "end": 1500, "text": "我们开始今天的话题", "segment_index": 2, "clip_index": 0},
    {"start": 1500, "end": 4500, "text": "欢迎来到访谈节目", "segment_index": 1, "clip_index": 1}
  ],
  "unused_segment_indices": [0]
}
```

返回的 `segments` 可以直接送入 `/layout/wrap`（下标字段会被忽略）：

```bash
curl -s -X POST http://localhost:8000/layout/wrap \
  -H "Content-Type: application/json" \
  -d '{
    "segments": [
      {"start": 0, "end": 1500, "text": "我们开始今天的话题", "segment_index": 2, "clip_index": 0},
      {"start": 1500, "end": 4500, "text": "欢迎来到访谈节目", "segment_index": 1, "clip_index": 1}
    ],
    "max_width": 8,
    "max_lines": 2
  }'
```

## 本地开发（不用容器）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   └── main.py          # FastAPI 应用、锚点推荐（全局最优匹配）、线性换算、字幕断行（全局最优排版）与剪辑投影逻辑
├── Dockerfile           # 构建期安装依赖，容器启动即启动服务
├── docker-compose.yml   # 一键编排
├── requirements.txt
└── README.md
```
