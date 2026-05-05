"""
===============================================================================
Excel 读取工具 (Excel Reader)
读取 model_gen4.xlsx 中的构型参数表。
- 从第3行开始读取（跳过前2行表头）
- 第1列：构型名称（整数）
- 第2-5列：4个参数
===============================================================================
"""
import openpyxl
from typing import Dict, List, Tuple
from utils.logger import setup_logger

logger = setup_logger(__name__)


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
    """
    logger.info(f"正在读取 Excel 参数表: {excel_path}")

    if not os.path.exists(excel_path):
        raise FileNotFoundError(f"Excel 文件不存在: {excel_path}")

    wb = openpyxl.load_workbook(excel_path, data_only=True)
    ws = wb.active

    configs: Dict[int, List[float]] = {}

    # 从第3行开始读取（openpyxl 行号从1开始）
    for row in ws.iter_rows(min_row=3, values_only=True):
        if row[0] is None:
            # 遇到空行则停止读取
            break

        try:
            config_name = int(row[0])  # 第1列：构型名称（整数）
            params = [float(row[i]) for i in range(1, 5)]  # 第2-5列：参数
            configs[config_name] = params
            logger.debug(f"  读取构型 {config_name}: 参数 = {params}")
        except (ValueError, TypeError, IndexError) as e:
            logger.warning(f"  跳过无效行: {row}, 错误: {e}")
            continue

    wb.close()
    logger.info(f"成功读取 {len(configs)} 个构型配置")
    return configs


def get_config_list(excel_path: str) -> List[int]:
    """
    获取所有构型名称的列表（按 Excel 中的出现顺序）。

    Args:
        excel_path: Excel 文件路径

    Returns:
        构型名称（整数）列表
    """
    configs = read_model_configs(excel_path)
    return list(configs.keys())
