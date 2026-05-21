"""
===============================================================================
Excel 读取工具 (Excel Reader)
读取 model_gen4.xlsx 中的构型参数表。
- 从第3行开始读取（跳过前2行表头）
- 第1列：构型名称（整数）
- 第2-5列：4个参数
===============================================================================
"""
import os
from typing import Dict, List
from utils.logger import setup_logger

logger = setup_logger(__name__)

# Excel 参数列范围（第2-5列，共4个参数）
PARAM_COLUMN_RANGE = range(1, 5)

# 延迟导入 openpyxl，仅在函数被调用时才导入，避免在模块导入时失败
_openpyxl_imported = False
_openpyxl = None

def _import_openpyxl():
    """延迟导入 openpyxl 库"""
    global _openpyxl_imported, _openpyxl
    if not _openpyxl_imported:
        try:
            import openpyxl as op
            _openpyxl = op
            _openpyxl_imported = True
        except ImportError as e:
            raise ImportError(
                "openpyxl 库未安装，请运行：pip install openpyxl"
            ) from e
    return _openpyxl


def read_model_configs(excel_path: str) -> Dict[int, List[float]]:
    """
    从 Excel 文件中读取所有构型的参数配置。

    Args:
        excel_path: Excel 文件的完整路径

    Returns:
        字典：{构型名称(整数): [参数1, 参数2, 参数3, 参数4]}

    Raises:
        FileNotFoundError: Excel 文件不存在
        ValueError: 数据格式错误
        ImportError: openpyxl 库未安装
    """
    openpyxl = _import_openpyxl()
    logger.info(f"正在读取 Excel 参数表: {excel_path}")

    if not os.path.exists(excel_path):
        raise FileNotFoundError(f"Excel 文件不存在: {excel_path}")

    configs: Dict[int, List[float]] = {}
    wb = None
    try:
        wb = openpyxl.load_workbook(excel_path, data_only=True)
        ws = wb.active

        # 从第3行开始读取（openpyxl 行号从1开始）
        for row in ws.iter_rows(min_row=3, values_only=True):
            if row[0] is None:
                # 遇到空行则停止读取
                break

            try:
                config_name = int(row[0])  # 第1列：构型名称（整数）
                params = [float(row[i]) for i in PARAM_COLUMN_RANGE]  # 第2-5列：参数
                configs[config_name] = params
                logger.debug(f"读取构型 {config_name}: 参数 = {params}")
            except (ValueError, TypeError, IndexError) as e:
                logger.warning(f"跳过无效行: {row}, 错误: {e}")
                continue
    finally:
        if wb is not None:
            wb.close()
    logger.info(f"成功读取 {len(configs)} 个构型配置")
    return configs
