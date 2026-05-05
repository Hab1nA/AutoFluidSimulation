# =============================================================================
# modules/__init__.py — 仿真流水线子模块包
#
# 子模块说明：
#   - state_manager       : SQLite 状态管理与断点续传逻辑（阶段1）
#   - solidworks_driver   : SolidWorks COM 接口自动化（阶段2）
#   - spaceclaim_driver   : SpaceClaim 命令行无头调用（阶段3）
#   - remote_scheduler    : SSH/SFTP 远程调度与异步求解（阶段4）
#   - tui_display         : Rich 终端可视化监控界面（阶段5）
#
# 使用方式：
#   from modules import StateManager, SolidWorksDriver, ...
#
# 项目主页：https://github.com/Hab1nA/AutoFluidSimulation
# =============================================================================
