# eos-log-to-rootcause

从 Java 报错日志堆栈反查分层应用（Spring Boot 分层 fatjar / war / 带 patch 目录的任何产品）的真实根因。

**产品无关**：不绑定任何厂商、包名或目录约定。介质布局、覆盖目录、应用命名空间、JDK/反编译器全部**运行时自动发现**。已在两套结构完全不同的介质上实测验证。

## 快速开始

```powershell
$PY    = 'python'
$SK    = "$PWD\scripts"
$MEDIA = '<产品安装目录>'

# 1. 看清介质布局（主归档 / 覆盖目录 / 命名空间）
& $PY -X utf8 "$SK\find_class.py" --media $MEDIA --layout

# 2. 定位类 + 用日志行号仲裁生效版本
& $PY -X utf8 "$SK\find_class.py" <FQCN> --media $MEDIA --method <方法> --line <日志行号>
```

## 工具链

| 脚本 | 作用 |
|---|---|
| `find_class.py` | 通用入口：布局发现 + 类定位 + 覆盖仲裁 + 行号指纹 |
| `media.py` | 自动发现 boot-layered / war / ear 布局，推断应用命名空间 |
| `env.py` | JDK / 反编译器运行时自动定位（纯 Python 后端不需要 JDK） |
| `classfile.py` | 纯 Python class 解析器（常量池→方法表→LineNumberTable），快 178 倍 |
| `javap_parse.py` | 统一解析入口：py / javap 双后端 |
| `lib_lookup.py` | 旧入口，委托 `find_class.py`（向后兼容） |
| `line_lookup.py` | 行号归属消歧与跨版本换算 |
| `fatjar_lookup.py` | 归档内定位与提取 |

## 测试

```powershell
& $PY -X utf8 "$SK\tests\run_tests.py"   # 四套：解析器全等 / 后端等价 / 行号边界 / 通用性
```

## 覆盖仲裁的通用模型

1. **覆盖目录内任何东西 > 主归档**（覆盖目录存在的唯一意义）；
2. **同目录内按文件名 ASCII 排序**（前导零由此自然生效）；
3. **行号指纹**：多份类体方法行号表不同时，日志行号唯一指认生效版本。

异常时人工指路（自动发现不灵时）：`EOS_OVERRIDE_DIRS` / `EOS_MAIN_ARCHIVE` / `EOS_MEDIA`。

## 只读铁律

只读分析：不修改产品介质，反编译产物与报告写入 `EOS_RC_WORK`（默认 `~/.log-to-rootcause/work`）。
