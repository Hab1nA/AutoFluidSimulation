# engine 包初始化。
# 注意：为避免循环导入（sw_executor ↔ daemon ↔ task_runner），
# 此处不进行便捷重导出。请直接从各子模块导入所需的类。
# 例如：
#   from engine.daemon import PipelineDaemon
#   from engine.scheduler import PipelineScheduler
