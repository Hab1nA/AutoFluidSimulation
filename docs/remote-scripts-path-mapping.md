# Remote Scripts 路径映射文档

> 本文档记录 `executor/remote_scripts/` 下所有 Fluent 配置文件中的路径引用，
> 包括已替换为占位符的路径和未替换的路径。

---

## 1. 占位符机制

本项目使用三种占位符，在两个不同阶段分层替换：

| 占位符 | 替换阶段 | 替换位置 | 替换为 |
|--------|---------|---------|--------|
| `{{REMOTE_ROOT}}` | `sync_scripts()` 上传时 | `remote_executor.py` | `REMOTE_CONFIG["scripts_dir"]`（如 `D:\xkz_1020`） |
| `{{REMOTE_SCDOC_DIR}}` | `sync_scripts()` 上传时 | `remote_executor.py` | `REMOTE_CONFIG["scdoc_dir"]` |
| `{{REMOTE_WORKING_DIR}}` | `sync_scripts()` 上传时 | `remote_executor.py` | `REMOTE_CONFIG["working_dir"]` |
| `{{REMOTE_REF_FILES_DIR}}` | `sync_scripts()` 上传时 | `remote_executor.py` | `REMOTE_CONFIG["ref_files_dir"]` |
| `{{SC_FILENAME}}` | `sync_scripts()` 上传时 | `remote_executor.py` | `STEP_FILE_PATTERNS["SC"]`（如 `model_gen4_{config}.scdoc`） |
| `{config}` | Fluent 运行时 | `batch_meshing_gen4.py` | 实际构型号（如 `5`） |

### 1.1 `sync_scripts()` 替换逻辑

所有占位符替换统一由 `_apply_placeholders()` 方法执行：

```python
# remote_executor.py → _apply_placeholders()
def _apply_placeholders(self, content: str) -> str:
    content = content.replace('{{REMOTE_ROOT}}', str(REMOTE_CONFIG["scripts_dir"]))
    content = content.replace('{{REMOTE_SCDOC_DIR}}', str(REMOTE_CONFIG["scdoc_dir"]))
    content = content.replace('{{REMOTE_WORKING_DIR}}', str(REMOTE_CONFIG["working_dir"]))
    content = content.replace('{{REMOTE_REF_FILES_DIR}}', str(REMOTE_CONFIG["ref_files_dir"]))
    content = content.replace('{{REMOTE_MSH_DIR}}', str(REMOTE_CONFIG["msh_dir"]))
    content = content.replace('{{REMOTE_RESULT_DIR}}', str(REMOTE_CONFIG["result_dir"]))
    sc_pattern = STEP_FILE_PATTERNS.get("SC", "")
    if sc_pattern:
        content = content.replace('{{SC_FILENAME}}', sc_pattern)
    return content
```

### 1.2 `batch_meshing_gen4.py` 替换逻辑

```python
content = content.replace('{config}', str(config_id))
```

### 1.3 三层层叠替换示例（以 `.wft` FileName 为例）

```
模板:       {{REMOTE_SCDOC_DIR}}/{{SC_FILENAME}}
                │                        │
    sync_scripts 替换               sync_scripts 替换
                ▼                        ▼
         D:\xkz_1020\scdoc\model_gen4_{config}.scdoc
                                                  │
                                       batch 运行时替换 {config}
                                                  ▼
         D:\xkz_1020\scdoc\model_gen4_5.scdoc
```

### 1.4 涉及的文件类型

| 扩展名 | 是否做占位符替换 | 说明 |
|--------|:---:|------|
| `.jou` | ✅ `{{REMOTE_ROOT}}` | Fluent Journal 脚本 |
| `.set` | ✅ `{{REMOTE_WORKING_DIR}}`、`{{REMOTE_REF_FILES_DIR}}` | Fluent 设置文件 |
| `.wft` | ✅ `{{REMOTE_SCDOC_DIR}}`、`{{SC_FILENAME}}` | Fluent Meshing 工作流模板 |
| `.pdf` | ✅ `{{REMOTE_REF_FILES_DIR}}` | Fluent PDF 表文件（文本格式） |
| `.py` | ❌ | Python 脚本已完全参数化 |
| `.inp` / `.dat` / `.fla` | ❌ | 纯数据文件 |

---

## 2. 已替换的占位符位置（共 14 处）

### 2.1 `solver_gen4.set`（7 处 `{{REMOTE_ROOT}}`）

| 行号 | 键/变量 | 占位符路径 |
|------|---------|-----------|
| 31 | `storage-dir` (animation-t) | `{{REMOTE_WORKING_DIR}}/animation-t` |
| 31 | `storage-dir` (animation-v) | `{{REMOTE_WORKING_DIR}}/animation-v` |
| 68 | `prepdf/default-thermo-db-fname` | `{{REMOTE_REF_FILES_DIR}}/chemkin-import_therm.dat` |
| 82 | `flamelet/filename` | `{{REMOTE_REF_FILES_DIR}}/model_gen4.fla` |
| 83 | `pdf/filename` | `{{REMOTE_REF_FILES_DIR}}/model_gen4.pdf` |
| 95 | `chemkin-names` 第 2 项 | `{{REMOTE_REF_FILES_DIR}}/chemkin-import_chem.inp` |
| 95 | `chemkin-names` 第 3 项 | `{{REMOTE_REF_FILES_DIR}}/chemkin-import_therm.dat` |

### 2.2 `meshing_gen4.jou`（1 处 `{{REMOTE_ROOT}}`）

| 行号 | 内容 | 占位符路径 |
|------|------|-----------|
| 2 | `workflow.LoadWorkflow(FilePath=...)` | `{{REMOTE_ROOT}}/meshing_gen4.wft` |

### 2.3 `solver_gen4.jou`（1 处 `{{REMOTE_ROOT}}`）

| 行号 | 内容 | 占位符路径 |
|------|------|-----------|
| 2 | `/file/read-settings` | `{{REMOTE_ROOT}}/solver_gen4.set` |

### 2.4 `meshing_gen4.wft`（2 处占位符）

| 行号 | 键 | 占位符路径 |
|------|-----|-----------|
| 7 | `FileName` (ImportGeometry) | `{{REMOTE_SCDOC_DIR}}/{{SC_FILENAME}}` |

- `{{REMOTE_SCDOC_DIR}}` → 由 `sync_scripts()` 替换为 `REMOTE_CONFIG["scdoc_dir"]`
- `{{SC_FILENAME}}` → 由 `sync_scripts()` 替换为 `STEP_FILE_PATTERNS["SC"]`
- `{config}` → 由 `batch_meshing_gen4.py` 替换为实际构型号

### 2.5 `fluent_chemkin_files/model_gen4.pdf`（3 处 `{{REMOTE_ROOT}}`）

| 行号 | 键 | 占位符路径 |
|------|-----|-----------|
| 42 | `pdf/thermo-file-name` | `{{REMOTE_REF_FILES_DIR}}/chemkin-import_therm.dat` |
| 48 | `pdf/flamelet-mechanism-file-name` | `{{REMOTE_REF_FILES_DIR}}/chemkin-import_chem.inp` |
| 49 | `pdf/flamelet-thermo-file-name` | `{{REMOTE_REF_FILES_DIR}}/chemkin-import_therm.dat` |

> **说明**：`.pdf` 是 Fluent 预生成的 PDF 表文件（文本格式，非 Adobe PDF）。
> `sync_scripts()` 上传时会通过 `_apply_placeholders()` 执行所有占位符替换。

---

## 3. 未做占位符替换的路径

### 3.1 `solver_gen4.set` 中的 ISAT / motion-history 路径

| 行号 | 键 | 当前值 | 未替换原因 |
|------|-----|--------|-----------|
| 96 | `species/isat-file` | `C:/Users/XKZ/Documents/000ansys_data/resource/Combustion_zone_model.35` | 路径格式不属于 `D:\xkz_1020` 模式 |
| 131 | `dynamesh/motion-history/basename` | `C:/Users/XKZ/Documents/000ansys_data/resource/Combustion_zone_model.35` | 同上 |

**说明**：这两个路径引用 Fluent ISAT 表文件，由 Fluent 在运行时自动生成/读取。在远程机器上不存在时 Fluent 会自动忽略或重建。

### 3.2 `solver_gen4.set` 中的 `species/kinetics/spe-ck-eq?`

| 行号 | 键 | 值 | 说明 |
|------|-----|-----|------|
| 94 | `species/kinetics/spe-ck-eq?` | `#t` | 布尔值，非路径 |

**说明**：此选项控制是否使用 Chemkin 风格的物种平衡方程计算，不是文件路径引用，无需替换。

### 3.3 `fluent_chemkin_files/model_gen4.fla`

经检查，该文件内部**不包含**任何 `D:\xkz_1020` 路径引用，无需替换。

---

## 4. 配置驱动说明

### `{{SC_FILENAME}}` 来源

| 配置位置 | 键 | 当前值 |
|---------|-----|--------|
| `autofluid_config.toml` | `[step_file_patterns].SC` | `model_gen4_{config}.scdoc` |
| `engine/config.py` | `STEP_FILE_PATTERNS["SC"]` | 同上（TOML 可覆盖默认值） |

如需更改 SC 文件命名规则，只需修改 `autofluid_config.toml` 中的 `SC` 值，
`.wft` 模板无需改动，`sync_scripts()` 会自动使用新的模板名。

### 远程目录配置来源

所有远程目录均在 `autofluid_config.toml` 的 `[remote_config]` 中独立配置：

| 配置键 | 含义 | 当前值 |
|--------|------|--------|
| `working_dir` | 仿真工作目录 | `D:\xkz_1020\workingdir` |
| `scripts_dir` | 远程脚本部署目录 | `D:\xkz_1020` |
| `ref_files_dir` | 仿真引用文件目录 | `D:\xkz_1020\fluent_chemkin_files` |
| `scdoc_dir` | SCDOC 接收目录 | `D:\xkz_1020\scdoc` |
| `msh_dir` | 网格输出目录 | `D:\xkz_1020\msh` |
| `result_dir` | 仿真输出目录 | `D:\xkz_1020\case` |
| `flag_dir` | 仿真标志目录 | `D:\xkz_1020\flags` |
| `mpi_bin_dir` | MPI安装目录 | `C:\Program Files\ANSYS Inc\v241\fluent\fluent24.1.0\multiport\mpi\win64\intel2021\bin` |

如需调整任何目录位置，只需修改 TOML 配置，脚本文件和代码无需改动。

---

## 5. 文件上传与 Hash 策略

### 文件上传分类

| 分类 | 文件扩展名 | 上传方式 |
|------|-----------|---------|
| 占位符替换 | `.jou`、`.set`、`.wft`、`.pdf` | 读取 → `_apply_placeholders()` → 写临时文件 → 上传 |
| 直接上传 | `.py`、`.inp`、`.dat`、`.fla` | 原样上传 |

### Hash 计算策略

| 文件类型 | Hash 计算方式 | 原因 |
|---------|-------------|------|
| `.jou`、`.set`、`.wft`、`.pdf` | 替换所有占位符后计算 | 与远程文件内容一致，避免每次重复上传 |
| 其他文件 | 原始内容直接计算 | 无替换逻辑 |
