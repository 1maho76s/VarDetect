# SharedVarFinder

适用于 C/C++ 代码的**共享变量检测**与 **SIM_FLUSH 插桩**工具。基于 Clang AST JSON dump 进行精准的共享变量识别，并自动在共享内存访问点插入 `SIM_FLUSH` / `SIM_FLUSH_TEMP` / `SIM_FLUSH_INTER_PTR` 调用。

---

## 目录

- [项目概览](#项目概览)
- [架构总览](#架构总览)
- [模块详解](#模块详解)
  - [cli.py — 命令行入口](#clipy--命令行入口)
  - [finder.py — 核心引擎](#finderpy--核心引擎)
  - [collect.py — 结构体收集器](#collectpy--结构体收集器)
- [插桩工作流详解](#插桩工作流详解)
  - [两阶段架构](#两阶段架构)
  - [数据流传播 (Phase 1)](#数据流传播-phase-1)
  - [AST 遍历与插桩 (Phase 2)](#ast-遍历与插桩-phase-2)
  - [后处理阶段](#后处理阶段)
- [关键特性深入](#关键特性深入)
  - [保守指针模式](#保守指针模式)
  - [多行 if 条件处理](#多行-if-条件处理)
  - [Post-if-flush](#post-if-flush)
  - [Temp Flush（源码级回退）](#temp-flush源码级回退)
  - [Inter-ptr-flush（中间指针 flush）](#inter-ptr-flush中间指针-flush)
  - [Cacheline 合并](#cacheline-合并)
  - [位域绕过](#位域绕过)
- [快速开始](#快速开始)
- [命令行参数](#命令行参数)
- [局限与已知问题](#局限与已知问题)

---

## 项目概览

### 目的

在多核/多线程系统中，共享变量的读写需要 cache flush 来保证一致性。**手动插入 `SIM_FLUSH`** 既繁琐又容易遗漏。本工具自动化这一过程：

1. **检测**：通过 Clang AST 精确定位 C/C++ 源码中所有共享变量（全局变量、静态变量）的访问点
2. **插桩**：在每个访问点自动插入对应的 `SIM_FLUSH(&(var))` 或 `SIM_FLUSH_TEMP(&(var))` 调用

### 效果

| 原始代码 | 插桩后 | 说明 |
|---------|--------|------|
| `g_counter++;` | `g_counter++; SIM_FLUSH(&(g_counter));` | AST 确认的访问 |
| `tp->rcv_bytes += len;` | `tp->rcv_bytes += len; SIM_FLUSH(&(tp->rcv_bytes));` | AST 确认的访问 |
| `tp->pkt_stat->rcved += len;` | `tp->pkt_stat->rcved += len; SIM_FLUSH(&(...->rcved)); SIM_FLUSH_INTER_PTR(&(tp->pkt_stat));` | 中间指针也 flush |
| `if (tp->state == RUNNING)` | `if (tp->state == RUNNING) { SIM_FLUSH_TEMP(&(tp->state)); ... }` | 函数未声明导致 RecoveryExpr，由 temp_flush 回退检测 |

---

## 架构总览

```
模块	职责	行数
models.py	数据类 (SharedVariable, VariableAccess)	42
clang_ast.py	Clang AST 执行与解析	89
shared_vars.py	共享变量检测 (AST + 文本 fallback)	228
access_detect.py	AST 变量访问检测 + 数据流分析	782
source_fallback.py	源码级 fallback (tokenize + regex 双引擎)	414
instrument.py	代码插桩 (flush 插入、括号平衡、post_if_flush)	694
utils.py	文件遍历、JSON 导出	44
cli.py	CLI 参数解析	101
__init__.py	公共 API 导出	8

                    ┌──────────────┐
                    │   cli.py     │  命令行入口, 参数解析
                    └──────┬───────┘
                           │
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
     find_shared_    instrument_      collect.py
     variables()     code()           (独立工具)
            │              │
            └──────┬───────┘
                   ▼
            ┌──────────────┐
            │  finder.py   │  核心引擎
            │              │
            │  ┌─────────────────────────┐
            │  │ 共享变量检测             │
            │  │ _find_shared_variables   │
            │  │ _from_ast()             │
            │  └─────────────────────────┘
            │  ┌─────────────────────────┐
            │  │ 插桩引擎                │
            │  │ _find_variable_accesses │
            │  │ _from_ast() + walk()   │
            │  └─────────────────────────┘
            │  ┌─────────────────────────┐
            │  │ 数据流传播              │
            │  │ _collect_shared_pointer │
            │  │ _aliases()             │
            │  └─────────────────────────┘
            │  ┌─────────────────────────┐
            │  │ 后处理                  │
            │  │ else-if 传播            │
            │  │ post-if-flush           │
            │  │ source fallback         │
            │  └─────────────────────────┘
            └──────────────────────────────┘
```

### 数据流

```
C/C++ 源码
    │
    ▼
Clang -ast-dump=json ──→ JSON AST
    │
    ├──→ _find_shared_variables_from_ast()  ──→ shared_vars: {g_counter, g_ctx, ...}
    │
    └──→ _find_variable_accesses_from_ast()
         │
         ├── 1. _collect_shared_pointer_aliases()  ──→ shared_pointer_vars: {tp, sp, info, ...}
         │
         ├── 2. walk() AST 遍历  ──→ VariableAccess 列表
         │       (RecoveryExpr 节点仅递归子节点, 不做正则扫描)
         │
         ├── 3. temp_flush 源码级回退 (可选)  ──→ SIM_FLUSH_TEMP 访问列表
         │       (统一处理 Clang 无法解析的区域, 含去重)
         │
         ├── 4. 去重 / cacheline 合并 / 位域过滤
         │
         ├── 5. inter_ptr_flush 中间指针展开 (可选)  ──→ SIM_FLUSH_INTER_PTR
         │
         ├── 6. 逐行插入 SIM_FLUSH / SIM_FLUSH_TEMP / SIM_FLUSH_INTER_PTR
         │
         ├── 7. else-if 链传播
         │
         └── 8. post-if-flush (可选)
                    │
                    ▼
              插桩后的源码
```

---

## 模块详解

### cli.py — 命令行入口

负责参数解析和两种模式的分发：

| 模式 | 触发条件 | 功能 |
|------|---------|------|
| **检测模式** | `paths` (无 `--instrument`) | 扫描文件/目录，输出共享变量列表（文本或 JSON） |
| **插桩模式** | `--instrument <file>` | 对单个文件插桩，输出到 `<name>_result.<ext>` |

### finder.py — 核心引擎

#### 1. 共享变量检测

遍历 Clang AST，在 `TranslationUnitDecl` / `NamespaceDecl` 作用域下查找 `VarDecl`，排除：
- `const` / `constexpr` 变量
- 来自 `#include` 头文件的变量
- 函数体内的局部变量

若 Clang 解析失败，回退到源码级正则扫描 (`_find_shared_variables_in_text_scanner`)。

#### 2. 插桩引擎

完整的插桩流水线：

```
instrument_code(source, ...)
    │
    ├── find_shared_variables_in_text()          # 步骤1: 找共享变量
    ├── _run_clang_ast_dump()                     # 步骤2: 生成 AST
    ├── _find_variable_accesses_from_ast()        # 步骤3: 找所有访问点
    │       ├── _collect_shared_pointer_aliases() #   3a: 指针别名传播
    │       ├── walk()                             #   3b: AST 遍历 (RecoveryExpr 仅递归子节点)
    │       └── temp_flush 源码回退 (可选)          #   3c: 正则扫描未解析区域
    ├── 去重 & 合并                                 # 步骤4
    ├── inter_ptr_flush 中间指针展开 (可选)          # 步骤5
    ├── 逐行插入 SIM_FLUSH / SIM_FLUSH_TEMP / SIM_FLUSH_INTER_PTR  # 步骤6
    ├── else-if 链传播                              # 步骤7
    ├── post-if-flush (可选)                        # 步骤8
    └── temp-flush 源码回退已整合到步骤3c            # (不再有独立步骤)
```

#### 3. 数据结构

```python
@dataclass
class SharedVariable:
    name: str          # 变量名
    kind: str          # "global" | "static" | "extern"
    file: str          # 文件名
    line: int          # 行号
    declaration: str   # 声明原文

@dataclass
class VariableAccess:
    name: str                  # 变量名
    line: int                  # 起始行
    column: int                # 起始列
    end_line: int              # 结束行
    is_write: bool             # 是否写操作
    expression: str            # 完整表达式, 如 "tp->rcv_bytes"
    root_name: str             # 根变量名, 如 "tp"
    root_expression: str       # 根表达式
    struct_type: Optional[str] # 结构体类型名
    member_name: Optional[str] # 成员名
    member_offset: Optional[int] # 成员偏移量 (字节)
    use_temp: bool             # 使用 SIM_FLUSH_TEMP 还是 SIM_FLUSH
    use_inter_ptr: bool        # 使用 SIM_FLUSH_INTER_PTR (中间指针读取)
```

#### 4. 关键辅助函数

| 函数 | 功能 |
|------|------|
| `_root_decl_name(node)` | 递归穿透 MemberExpr/ArraySubscriptExpr/CastExpr 找到根 DeclRefExpr 的名字 |
| `_node_has_shared_reference(node, ancestors)` | 判断 AST 节点是否引用了共享内存 |
| `_is_shared_address_of(node)` | 判断表达式是否是"共享变量的地址"（支持 MemberExpr, ArraySubscriptExpr） |
| `_is_source_node(node)` | 判断节点是否来自源文件（排除 include 文件） |
| `_get_enclosing_end_line(ancestors, default)` | 向上查找 CallExpr/BinaryOperator 确定 flush 插入的结束行 |
| `_find_global_var_accesses_from_source(...)` | 源码级正则回退：扫描 `ptr->member.field` 链和 `*ptr`，产生 SIM_FLUSH_TEMP |
| `_expand_inter_ptr_prefixes(expr)` | 对 `a->b->c->d` 返回中间指针前缀列表 `['a->b', 'a->b->c']` |

### collect.py — 结构体收集器

独立工具，从目录中扫描所有 `.h/.hpp` 头文件，提取结构体定义。

**两个子功能**：

| 命令 | 输出 | 用途 |
|------|------|------|
| `--all` | `.txt` + `.h` + `_bitfield.json` | 全部三个输出 |
| `--output-header` | `.h` 合并头文件 | 给 `--struct-header` 用 |

**bitfield map 格式**：
```json
{
  "struct ase_tcp_port": {
    "bypass": true,
    "write_brake": true,
    "rcv_bytes": false
  }
}
```

`true` 表示该成员是位域（不能取地址，跳过插桩），`false` 表示普通成员。

---

## 插桩工作流详解

### 两阶段架构

插桩的核心是**两阶段分析**：

#### Phase 1: `_collect_shared_pointer_aliases` — 数据流传播

**目的**：构建 `shared_pointer_vars` 集合 — 所有从共享变量派生的局部指针变量。

```
输入: AST
输出: shared_pointer_vars = {link, tp, sp, info, ...}

规则:
  VarDecl + 指针类型 + 初始化器来自共享内存  →  加入
  BinaryOperator = 右侧来自共享内存          →  加入
```

**传播链路示例**：

```c
void func(struct list_head *link) {   // link ∈ shared_pointer_vars (conservative_ptr)
    struct ase_tcp_port *sp = list_entry(link, ...);  
    // → sp ∈ shared_pointer_vars (VarDecl Case 3: _root_decl_name(init)="link")

    struct ase_xbuf_info *info;       // 声明无 init, 暂不追踪
    list_for_each_entry_safe(info, str, &sp->pkt_list, link) {
    // → info ∈ shared_pointer_vars (BinaryOperator: _root_decl_name(rhs)="sp")
```

**VarDecl 的四个追踪 case**：

| Case | 模式 | 示例 |
|------|------|------|
| 1 | inner 中含有 `UnaryOperator(&)` | `struct T *p = &shared_var;` |
| 2 | init 直接是 `UnaryOperator(&)` | `int *p = &g_shared;` |
| 3 | 通过 `_root_decl_name` 检查根变量 | `struct T *p = tp->peer.psp;` |
| 4 | init 是字符串且恰好是共享指针名 | clang 简化 AST 的特殊情况 |

**BinaryOperator 追踪**：处理声明时未初始化、后续才赋值的指针：

```c
struct T *p = NULL;
p = tp->peer;    // 此时 p 被加入 shared_pointer_vars
```

#### Phase 2: `walk` — AST 遍历与访问检测

遍历 AST，对每个节点调用：

```
DeclRefExpr → _is_shared_declref(name) → 直接全局共享变量
MemberExpr   → _node_has_shared_reference → 成员访问(支持多级 ->)
UnaryOperator(*p) → _node_has_shared_reference → 指针解引用
ArraySubscriptExpr → _node_has_shared_reference → 数组下标
RecoveryExpr → 仅递归遍历子节点 (正则扫描已移至 temp_flush 源码回退统一处理)
```

**`_node_has_shared_reference` 的判断逻辑**：

```
DeclRefExpr:
  ├─ _is_shared_declref(node) → True   (全局共享变量)
  └─ _is_shared_pointer_declref(node) → 检查 ancestors 中是否有
       * 解引用 (*) 或 -> 成员访问 → True  (局部指针指向共享内存)

MemberExpr:
  └─ 递归检查 base: _node_has_shared_reference(base)

UnaryOperator(*):
  └─ 递归检查 subExpr

ImplicitCastExpr/ParenExpr/CastExpr:
  └─ 穿透, 递归检查 inner
```

### 后处理阶段

#### 阶段 A: 去重与合并

访问点按 `(line, column, expression)` 去重后，进行两种合并：

1. **数组 cacheline 合并**：同一 cacheline（16 个元素 = 64 字节）内的多个数组下标访问合并为一个：
   ```c
   arr[0] = x; arr[1] = y; arr[2] = z;
   // → SIM_FLUSH(&(arr[0]));  // 一次 flush 覆盖整个 cacheline
   ```

2. **结构体 cacheline 合并**：同一 64 字节 cacheline 内的多个成员访问合并为一个。

3. **→ 链去重**：`tp->peer.out` 作为完整表达式时，丢弃 `tp->peer` 这个子表达式（避免重复 flush）。

#### 阶段 B: 逐行插入

按照 `end_line` 从大到小的顺序逐行插入。插入逻辑包括：
- **裸函数体包裹**：当 `if/for/while` 后跟的是无花括号的单行语句时，自动包裹花括号并把 flush 放入
- **跨行表达式续行跳过**：跳过 `,` `(` `&&` `||` 续行，避免 flush 插入到条件表达式内部
- **括号平衡检测**：从 access 所在行到插入点计算括号深度，若 `(` 未闭合则继续向下扫描直到平衡或遇到 `;`，防止 flush 插入多行函数/宏调用中间
- **return 语句跳过**：`temp_flush` 正则回退会跳过 `return` 语句内的所有行（从 `return` 到 `;`）
- **`{` 和 `) {` 检测**：flush 正确放入花括号内部

#### 阶段 C: else-if 链传播

`if` 条件中的共享变量 flush 传播到同一链的 `else if` 和 `else` 分支中。

#### 阶段 D: post-if-flush (可选)

将条件中共享变量的 `SIM_FLUSH` 从 `if` 函数体内部移动到整个 `if/else-if/else` 链的闭合 `}` 之后。

#### 阶段 E: temp-flush 源码级回退 (可选)

当 Clang 因函数未声明等原因无法解析类型时，整个表达式变为 `RecoveryExpr`，其中的子表达式（即使类型定义完整）也会丢失。源码级回退统一处理这些区域：

- 正则扫描共享指针变量的 `->` 和 `.` 成员链（如 `tp->sndbuf.len`）
- 检测指针解引用（`*ptr`）和赋值操作
- 产生 `SIM_FLUSH_TEMP` 标记，与 AST 确认的 `SIM_FLUSH` 区分
- 通过 `(line, expression)` 和 `(end_line, expression)` 两个维度去重，避免与 AST 路径重复

---

## 关键特性深入

### 保守指针模式 (`--conservative-ptr`)

**原理**：将所有指针类型的函数参数视为潜在共享变量指针。

```c
void handle(struct ase_tcp_port *tp) {  // tp 被标记为共享指针
    tp->rcv_bytes += len;               // → SIM_FLUSH(&(tp->rcv_bytes));
    tp->pkt_stat->rcved += len;         // → SIM_FLUSH(&(tp->pkt_stat));
}
```

**实现**：
- `_collect_conservative_ptr_params` 遍历 AST 中所有 `ParmVarDecl`（指针类型），加入 `shared_pointer_vars`
- 后续所有 `tp->xxx` 访问都被 `_node_has_shared_reference` 识别为共享内存访问
- 配合数据流传播，`tp` 派生的局部指针也自动被追踪

### 多行 if 条件处理

**问题**：对于跨越多行的 `if` 条件表达式，原始实现会把第三行条件片段误判为"裸函数体"并包裹花括号。

**修复**：两处关键改动：

1. **`wrapped_bare_body` 条件** — 增加 `&&`/`||` 结尾检查：
   如果 `if` 行以 `&&` 结尾，说明条件未结束，下一行是条件续行而非函数体。

2. **`not wrapped_bare_body` 分支** — 增加续行跳过和 `) {` 检测：
   - 新增 `while` 循环跳过 `&&`/`||` 续行
   - `next_line.strip().endswith('{') and ')' in next_line` → 多行条件末尾的 `) {` 视同独立 `{`

### Post-if-flush

**原理**：
```c
// 插桩后 (post-if-flush 生效):
if (tp->state == RUNNING) {
    body();
}
SIM_FLUSH(&(tp->state));  // flush 移到 } 之后
```

**实现**：`post_if_flush` 后处理模块扫描所有 `if` 语句，找到条件中共享变量的 `SIM_FLUSH`，追踪 `if/else-if/else` 链找到最终闭合 `}`，将 flush 移到其后。正确处理多行 `if` 条件和 `} else if (...) {` 模式。

### Temp Flush（源码级回退）

当 Clang 因缺少头文件（如函数未声明）无法解析类型时，会产生 `RecoveryExpr` 节点。Clang 的错误恢复是整体性的——一个未知函数调用会导致包含它的整个表达式变成 `RecoveryExpr`，连带其中本可以解析的子表达式（如 `tp->in`、`tp->sndbuf.len`）一起丢失。

启用 `--temp-flush` 后，工具使用源码级正则扫描作为统一的回退机制：

```
扫描规则:
  - 共享指针变量的 -> 和 . 成员链:  ptr_name->member1.member2
  - 指针解引用:                     *ptr_name
```

这些访问点使用 `SIM_FLUSH_TEMP` 标记（区别于 AST 确认的 `SIM_FLUSH`），表示"无法通过类型系统确认，由正则猜测得到"。

**架构说明**：`RecoveryExpr` 节点不再做独立的正则扫描。当 `--temp-flush` 开启时，由源码级回退统一处理所有 Clang 无法解析的区域；当 `--temp-flush` 关闭时，这些区域不会被插桩（用户不想要不确定的插桩）。这避免了两条检测路径的冲突和重复。

**去重机制**：源码回退检测到的访问会与 AST 路径已检测到的访问去重——同一 `(line, expression)` 或同一 `(end_line, expression)` 的重复访问会被过滤。

### Inter-ptr-flush（中间指针 flush）

**问题**：对于链式指针访问 `tp->pkt_stat->rcved += len`，工具只 flush 叶子表达式 `tp->pkt_stat->rcved`。但中间指针 `tp->pkt_stat` 本身也是共享内存中的一个值——如果指针过期，后续解引用可能指向错误地址。

**解决**：启用 `--inter-ptr-flush` 后，对每个包含多级 `->` 的访问表达式，自动生成中间指针的 flush：

```c
tp->pkt_stat->rcved += len;
SIM_FLUSH(&(tp->pkt_stat->rcved));       // 叶子写入
SIM_FLUSH_INTER_PTR(&(tp->pkt_stat));    // 中间指针读取
```

**规则**：
- 只在 `->` 处拆分（指针解引用），`.` 不拆分（结构体成员访问无独立指针读取）
- 对 `a->b->c->d` 生成 `['a->b', 'a->b->c']` 两个中间前缀
- 叶子表达式本身不重复（已有 SIM_FLUSH/SIM_FLUSH_TEMP）
- 与已有访问按 `(end_line, expression)` 去重
- 独立于 `--temp-flush`，可单独使用

**实现**：`_expand_inter_ptr_prefixes(expr)` 函数扫描表达式中所有 `->` 的位置，对除最后一个 `->` 外的每个位置提取前缀。

### Cacheline 合并

**目的**：减少 flush 调用次数，一次 flush 覆盖整个 64 字节 cacheline。

- 数组：同一 cacheline（`idx // 16` 相同）的多次访问合并为一次
- 结构体：同一 cacheline（`offset // 64` 相同）的多个成员访问合并为一次

### 位域绕过

C 语言不允许取位域成员的地址。工具通过 `--bitfield-map` 跳过这些成员的插桩。

---

## 快速开始

### 安装

```bash
python3 -m pip install -e .
```

**前置依赖**：`clang` 必须在 PATH 中。

### 基础用法

```bash
# 1. 收集结构体定义
python3 src/sharedvarfinder/collect.py srcFile --all -o srcFile/all_structs

# 2. 插桩单个文件
# 2.1 --engine tokenize（默认）— 词法分析方案，使用 finder_tokenize.py
#     --engine regex — 旧的正则匹配方案，使用 finder.py
PYTHONPATH=src python3 -m sharedvarfinder.cli --instrument testFolder/testSrc/ase_tcp2.c \
    -I testFolder/testSrc \
    --conservative-ptr \
    --post-if-flush \
    --bitfield-map testFolder/collectStructs/all_structs_bitfield.json \
    --struct-header testFolder/collectStructs/all_structs.h \
    --temp-flush \
    --inter-ptr-flush \
    -o testFolder/testResult \
    --engine tokenize

# 输出: srcFile/ase_tcp_result.c
```

### 仅检测（不插桩）

```bash
# 文本输出
PYTHONPATH=src python3 -m sharedvarfinder srcFile/

# JSON 输出
PYTHONPATH=src python3 -m sharedvarfinder srcFile/ --json
```

---

## 命令行参数

### 基础

| 参数 | 说明 |
|------|------|
| `paths` | 要扫描的文件或目录（必填） |
| `-r, --recursive` | 递归扫描目录 |
| `--json` | JSON 格式输出共享变量列表 |

### 插桩

| 参数 | 说明 |
|------|------|
| `--instrument` | 启用插桩模式（需要恰好一个文件路径） |
| `-I, --include` | Clang AST 解析的 include 路径（可重复） |

### 共享变量检测

| 参数 | 说明 |
|------|------|
| `--conservative-ptr` | 所有指针类型函数参数都视为潜在共享变量 |
| `--bitfield-map <path>` | 位域成员映射 JSON（跳过不能取地址的位域） |

### Flush 放置

| 参数 | 说明 |
|------|------|
| `--post-if-flush` | 条件 SIM_FLUSH 移到 if/else-if/else 链之后 |

### 类型解析

| 参数 | 说明 |
|------|------|
| `--struct-header <path>` | 合并的结构体头文件（Clang 解析时强制 include） |

### 源码级回退

| 参数 | 说明 |
|------|------|
| `--temp-flush` | 启用源码级正则扫描回退：对 Clang 无法解析的区域（RecoveryExpr）用正则检测共享变量访问，标记为 SIM_FLUSH_TEMP。不开启时这些区域不会被插桩 |
| `--inter-ptr-flush` | 对链式 `->` 访问的中间指针生成 SIM_FLUSH_INTER_PTR。独立于 `--temp-flush`，可单独使用 |

---

## 局限与已知问题

### 数据流分析

1. **不做过程间分析**：函数调用返回值不追踪。`struct T *p = get_shared_ptr()` 中的 `p` 不会被标记。
2. **非指针局部变量不追踪**：`int x = tp->counter` — `x` 在本地栈上，不需要 flush。正确行为。
3. **单向传播**：跟踪 `tp → p → q` 的赋值链，但不跟踪 `p = tp->peer; q = p;` 的双跳（需 `q` 被独立检测到）。

### AST 解析

4. **依赖 Clang**：需要系统安装 Clang 并在 PATH 中。
5. **类型解析依赖头文件**：缺少头文件时 Clang 生成 `RecoveryExpr`，整个表达式（包括其中本可解析的子表达式）都会丢失。启用 `--temp-flush` 后由源码级正则统一回退处理，产生 `SIM_FLUSH_TEMP`。未声明的函数会导致包含它的整个条件/表达式变为 `RecoveryExpr`。
6. **GNU 扩展**：`({...})` 语句表达式（如 `container_of` 宏）可能产生 `StmtExpr`，`_root_decl_name` 不直接处理此节点，但外层的类型转换（`CStyleCastExpr`）通常能穿透。

### 插桩精度

7. **宏展开**：宏定义中的共享变量可能被多次计数或漏计。
8. **跨行表达式**：已修复 `&&`/`||` 续行和多行函数/宏调用（括号平衡检测）问题。`return` 语句内不插桩。极深度嵌套的跨行表达式可能存在边界情况。
9. **行号漂移**：大量插入 `SIM_FLUSH` 后，行号会偏移，后续插入使用动态更新的 `lines` 列表，但复杂场景下可能有偏差。

### 性能

10. **大文件**：AST JSON dump 可能很大（数 MB），解析时间随文件大小线性增长。
11. **内存**：完整 AST 和中间数据结构驻留内存。

