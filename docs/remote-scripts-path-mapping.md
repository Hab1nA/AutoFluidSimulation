# Remote Scripts 路径映射文档

> 本文档记录 `executor/remote_scripts/` 下所有 Fluent 配置文件中的路径引用，
> 包括已替换为占位符的路径和未替换的路径。

---

## 1. 占位符替换机制

### 占位符格式

```
{{REMOTE_ROOT}}
```

### 替换时机

`executor/remote_executor.py` 的 `sync_scripts()` 方法在上传文件到远程工作站时执行替换。

### 替换逻辑

```python
content = content.replace('{{REMOTE_ROOT}}', remote_root)
```

其中 `remote_root` 取自 `REMOTE_CONFIG["root_dir"]`（当前值：`D:\xkz_1020`）。

### 涉及的文件类型

| 扩展名 | 是否做占位符替换 | 说明 |
|--------|:---:|------|
| `.jou` | ✅ | Fluent Journal 脚本 |
| `.set` | ✅ | Fluent 设置文件 |
| `.wft` | ✅ | Fluent Meshing 工作流模板 |
| `.py` | ❌ | Python 脚本已完全参数化，无硬编码路径 |
| `.inp` / `.dat` / `.fla` / `.pdf` | ❌ | 纯数据文件，直接上传不做文本替换 |

---

## 2. 已替换的占位符位置（共 10 处）

### 2.1 `solver_gen4.set`（7 处）

| 行号 | 键/变量 | 占位符路径 | 远程替换后（示例） |
|------|---------|-----------|-------------------|
| 31 | `storage-dir` (animation-t) | `{{REMOTE_ROOT}}/workingdir/animation-t` | `D:\xkz_1020\workingdir\animation-t` |
| 31 | `storage-dir` (animation-v) | `{{REMOTE_ROOT}}/workingdir/animation-v` | `D:\xkz_1020\workingdir\animation-v` |
| 68 | `prepdf/default-thermo-db-fname` | `{{REMOTE_ROOT}}/fluent_chemkin_files/chemkin-import_therm.dat` | `D:\xkz_1020\fluent_chemkin_files\chemkin-import_therm.dat` |
| 82 | `flamelet/filename` | `{{REMOTE_ROOT}}/fluent_chemkin_files/model_gen4.fla` | `D:\xkz_1020\fluent_chemkin_files\model_gen4.fla` |
| 83 | `pdf/filename` | `{{REMOTE_ROOT}}/fluent_chemkin_files/model_gen4.pdf` | `D:\xkz_1020\fluent_chemkin_files\model_gen4.pdf` |
| 95 | `chemkin-names` 第 2 项 | `{{REMOTE_ROOT}}/fluent_chemkin_files/chemkin-import_chem.inp` | `D:\xkz_1020\fluent_chemkin_files\chemkin-import_chem.inp` |
| 95 | `chemkin-names` 第 3 项 | `{{REMOTE_ROOT}}/fluent_chemkin_files/chemkin-import_therm.dat` | `D:\xkz_1020\fluent_chemkin_files\chemkin-import_therm.dat` |

#### 涉及的 6 个唯一远程文件

| 文件名 | 来源 | 远程路径 |
|--------|------|---------|
| `chemkin-import_chem.inp` | `fluent_chemkin_files/` | `{remote_root}/fluent_chemkin_files/chemkin-import_chem.inp` |
| `chemkin-import_therm.dat` | `fluent_chemkin_files/` | `{remote_root}/fluent_chemkin_files/chemkin-import_therm.dat` |
| `model_gen4.fla` | `fluent_chemkin_files/` | `{remote_root}/fluent_chemkin_files/model_gen4.fla` |
| `model_gen4.pdf` | `fluent_chemkin_files/` | `{remote_root}/fluent_chemkin_files/model_gen4.pdf` |
| `model_gen4.fla`（flamelet） | 同上 | 同上 |
| 动画目录 | 运行时生成 | `{remote_root}/workingdir/animation-{t,v}` |

### 2.2 `meshing_gen4.jou`（1 处）

| 行号 | 内容 | 占位符路径 | 远程替换后 |
|------|------|-----------|-----------|
| 2 | `workflow.LoadWorkflow(FilePath=...)` | `{{REMOTE_ROOT}}/meshing_gen4.wft` | `D:\xkz_1020\meshing_gen4.wft` |

### 2.3 `solver_gen4.jou`（1 处）

| 行号 | 内容 | 占位符路径 | 远程替换后 |
|------|------|-----------|-----------|
| 2 | `/file/read-settings` | `{{REMOTE_ROOT}}/solver_gen4.set` | `D:\xkz_1020\solver_gen4.set` |

### 2.4 `meshing_gen4.wft`（1 处）

| 行号 | 键 | 占位符路径 | 远程替换后 |
|------|-----|-----------|-----------|
| 7 | `FileName` (ImportGeometry) | `{{REMOTE_ROOT}}/scdoc/model_gen4_100.scdoc` | `D:\xkz_1020\scdoc\model_gen4_100.scdoc` |

> **注意**：`.wft` 中的 `model_gen4_100.scdoc` 在运行时会被 `batch_meshing_gen4.py` 进一步替换为
> `model_gen4_{config_id}.scdoc`（纯文本 `model_gen4_100` → `model_gen4_{config_id}`）。

---

## 3. 未做占位符替换的路径

### 3.1 `solver_gen4.set` 中的 ISAT / motion-history 路径

| 行号 | 键 | 当前值 | 未替换原因 |
|------|-----|--------|-----------|
| 96 | `species/isat-file` | `C:/Users/XKZ/Documents/000ansys_data/resource/Combustion_zone_model.35` | 路径格式不属于 `D:\xkz_1020` 模式 |
| 131 | `dynamesh/motion-history/basename` | `C:/Users/XKZ/Documents/000ansys_data/resource/Combustion_zone_model.35` | 同上 |

**说明**：
- 这两个路径引用的是 Fluent ISAT（In Situ Adaptive Tabulation）表文件
- 该文件由 Fluent 在运行时自动生成/读取，属于运行时缓存
- 路径指向本地机器的 `C:\Users\XKZ\...`，在远程机器上不存在
- Fluent 会在远程机器上忽略此路径或自动重建 ISAT 表
- **如需支持**：需要新增独立的替换规则（不能用 `{{REMOTE_ROOT}}`，因为路径前缀完全不同）

### 3.2 `model_gen4.pdf` 内部嵌入的路径

`fluent_chemkin_files/model_gen4.pdf` 是 Fluent 二进制 PDF 表文件，内部嵌入了以下路径：

| 行号（文本模式） | 键 | 值 |
|----------------|-----|-----|
| 42 | `pdf/thermo-file-name` | `D:/xkz_1020/fluentchemkinfiles/chemkin-import_therm.dat` |
| 48 | `pdf/flamelet-mechanism-file-name` | `D:/xkz_1020/fluentchemkinfiles/chemkin-import_chem.inp` |
| 49 | `pdf/flamelet-thermo-file-name` | `D:/xkz_1020/fluentchemkinfiles/chemkin-import_therm.dat` |

**说明**：
- `.pdf` 文件是 Fluent 预生成的 PDF 表（非 Adobe PDF），属于二进制格式
- 上传时不做文本替换（`fluent_chemkin_files/` 子目录文件直接原样上传）
- 这些嵌入路径是 Fluent 内部引用，Fluent 在读取 `.set` 文件时会用 `.set` 中的
  `chemkin-names` 和 `prepdf/default-thermo-db-fname` 覆盖这些内部路径
- **因此不影响运行**：只要 `.set` 文件中的占位符正确替换，Fluent 会优先使用 `.set` 中的路径

### 3.3 `solver_gen4.set` 中的 `species/kinetics/spe-ck-eq?`

| 行号 | 键 | 值 | 说明 |
|------|-----|-----|------|
| 94 | `species/kinetics/spe-ck-eq?` | `#t` | 布尔值，非路径 |

**说明**：此选项控制是否使用 Chemkin 风格的物种平衡方程计算，不是文件路径引用，无需替换。

---

## 4. 替换流程总览

```
本地模板文件                    sync_scripts 上传时              远程文件
─────────────                  ────────────────              ──────────
{{REMOTE_ROOT}}/xxx      ──→   replace('{{REMOTE_ROOT}}',   ──→  D:\xkz_1020\xxx
                                  remote_root)

model_gen4_100.scdoc     ──→   （不修改，直接上传）           ──→  model_gen4_100.scdoc
                                       │
                                       │ batch_meshing_gen4.py 运行时
                                       ▼
                                  model_gen4_{config_id}.scdoc
```

### 文件上传分类

| 分类 | 文件 | 上传方式 |
|------|------|---------|
| 占位符替换 | `.jou`、`.set`、`.wft` | 读取 → 替换 `{{REMOTE_ROOT}}` → 写临时文件 → 上传 |
| 直接上传 | `.py`、`.inp`、`.dat`、`.fla`、`.pdf` | 原样上传 |

### Hash 计算策略

| 文件类型 | Hash 计算方式 | 原因 |
|---------|-------------|------|
| `.jou`、`.set`、`.wft` | 替换 `{{REMOTE_ROOT}}` 后计算 | 与远程文件内容一致，避免每次重复上传 |
| 其他文件 | 原始内容直接计算 | 无替换逻辑 |
