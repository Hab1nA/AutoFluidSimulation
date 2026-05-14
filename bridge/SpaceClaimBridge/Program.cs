using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;

namespace AutoFluidSimulation.Bridge
{
    /// <summary>
    /// SpaceClaim Bridge — C# 中间衔接程序
    ///
    /// 功能：通过 SpaceClaim .NET API 的 Application.RunScript() 方法，
    ///        在 SpaceClaim 进程内部执行 Python 转换脚本（STEP → SCDOC），
    ///        实现 Python 主项目与 SpaceClaim 软件的可靠通信。
    ///
    /// 用法：
    ///   SpaceClaimBridge.exe
    ///     --script   &lt;transit脚本路径&gt;
    ///     --config   &lt;构型编号&gt;
    ///     --stepdir  &lt;STEP文件目录&gt;
    ///     --scdocdir &lt;SCDOC输出目录&gt;
    ///     [--timeout &lt;秒数, 默认300&gt;]
    ///     [--sc-exe   &lt;SpaceClaim.exe路径&gt;]
    ///
    /// 返回值：
    ///   0 = 成功
    ///   1 = 脚本执行失败
    ///   2 = SpaceClaim 连接失败
    ///   3 = 输出文件验证失败
    ///   4 = 参数错误
    ///   5 = 超时
    ///
    /// 设计原理：
    ///   - 使用 SpaceClaim 官方 API Application.RunScript() 替代不可靠的命令行 /RunScript 开关
    ///   - 通过 COM 互操作连接 SpaceClaim 应用程序实例
    ///   - 支持启动新实例或连接到已有实例
    ///   - 同步等待脚本执行完成，并通过 result 输出参数验证执行状态
    /// </summary>
    class Program
    {
        // SpaceClaim COM ProgID 候选列表（按优先级排序）
        private static readonly string[] SpaceClaimProgIds =
        {
            "SpaceClaim.Application",
            "SpaceClaim.Application.V23",
            "SCDM.Application",
        };

        static int Main(string[] args)
        {
            try
            {
                var options = ParseArguments(args);
                if (options == null)
                {
                    return 4;
                }

                return Execute(options);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[BRIDGE_FATAL] 未处理的异常: {ex}");
                return 1;
            }
        }

        /// <summary>
        /// 命令行参数解析
        /// </summary>
        private static BridgeOptions ParseArguments(string[] args)
        {
            var options = new BridgeOptions();

            for (int i = 0; i < args.Length; i++)
            {
                switch (args[i].ToLowerInvariant())
                {
                    case "--script":
                        if (++i < args.Length) options.ScriptPath = args[i];
                        break;
                    case "--config":
                        if (++i < args.Length) options.ConfigName = args[i];
                        break;
                    case "--stepdir":
                        if (++i < args.Length) options.StepDir = args[i];
                        break;
                    case "--scdocdir":
                        if (++i < args.Length) options.ScdocDir = args[i];
                        break;
                    case "--timeout":
                        if (++i < args.Length && int.TryParse(args[i], out int t))
                            options.TimeoutSeconds = t;
                        break;
                    case "--sc-exe":
                        if (++i < args.Length) options.ScExePath = args[i];
                        break;
                    case "--headless":
                        options.Headless = true;
                        break;
                    default:
                        Console.Error.WriteLine($"[BRIDGE_ERROR] 未知参数: {args[i]}");
                        PrintUsage();
                        return null;
                }
            }

            if (string.IsNullOrEmpty(options.ScriptPath) ||
                string.IsNullOrEmpty(options.ConfigName) ||
                string.IsNullOrEmpty(options.StepDir) ||
                string.IsNullOrEmpty(options.ScdocDir))
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] 缺少必要参数");
                PrintUsage();
                return null;
            }

            return options;
        }

        private static void PrintUsage()
        {
            Console.Error.WriteLine(@"
用法: SpaceClaimBridge.exe
  --script   <transit脚本路径>    (必需)
  --config   <构型编号>           (必需)
  --stepdir  <STEP文件目录>       (必需)
  --scdocdir <SCDOC输出目录>      (必需)
  [--timeout <秒数>]              (可选, 默认300)
  [--sc-exe  <SpaceClaim.exe路径>] (可选, 自动检测)
  [--headless]                    (可选, 无头模式)
");
        }

        /// <summary>
        /// 核心执行逻辑
        /// </summary>
        private static int Execute(BridgeOptions opts)
        {
            Console.WriteLine($"[BRIDGE] SpaceClaim Bridge 启动");
            Console.WriteLine($"[BRIDGE]   脚本: {opts.ScriptPath}");
            Console.WriteLine($"[BRIDGE]   构型: {opts.ConfigName}");
            Console.WriteLine($"[BRIDGE]   STEP目录: {opts.StepDir}");
            Console.WriteLine($"[BRIDGE]   SCDOC目录: {opts.ScdocDir}");
            Console.WriteLine($"[BRIDGE]   超时: {opts.TimeoutSeconds}s");

            // 验证输入文件
            var stepFile = Path.Combine(opts.StepDir, $"model_gen4.SLDPRT_{opts.ConfigName}.step");
            if (!File.Exists(stepFile))
            {
                Console.Error.WriteLine($"[BRIDGE_ERROR] STEP 文件不存在: {stepFile}");
                return 3;
            }
            Console.WriteLine($"[BRIDGE]   STEP文件: {stepFile} ({new FileInfo(stepFile).Length} bytes)");

            // 确保输出目录存在
            Directory.CreateDirectory(opts.ScdocDir);

            // 阶段1: 确保 SpaceClaim 运行并就绪
            Console.WriteLine("[BRIDGE] 正在连接 SpaceClaim...");
            dynamic scApp = ConnectToSpaceClaim(opts);
            if (scApp == null)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] 无法连接到 SpaceClaim");
                return 2;
            }
            Console.WriteLine("[BRIDGE] ✓ SpaceClaim 已连接");

            // 阶段2: 准备脚本参数字典
            var scriptParams = new Dictionary<string, object>
            {
                { "config_name", opts.ConfigName },
                { "step_dir", opts.StepDir },
                { "scdoc_dir", opts.ScdocDir },
            };

            // 阶段3: 调用 Application.RunScript 执行转换脚本
            // 注意：RunScript 的 API 签名为 void RunScript(string, IDictionary, out object)
            //       不可捕获返回值赋给 bool（会触发 RuntimeBinderException）
            //       result out 参数：脚本被找到并成功执行时返回 true
            Console.WriteLine($"[BRIDGE] 正在执行脚本: {Path.GetFileName(opts.ScriptPath)}");
            var stopwatch = Stopwatch.StartNew();

            try
            {
                object runResult = null;
                scApp.RunScript(opts.ScriptPath, scriptParams, out runResult);

                stopwatch.Stop();
                Console.WriteLine($"[BRIDGE] 脚本执行完成 (耗时 {stopwatch.Elapsed.TotalSeconds:F1}s)");
                Console.WriteLine($"[BRIDGE]   RunScript 完成, result={runResult}");

                // result 输出参数：脚本被找到并成功运行时返回 true（可能为 bool 或 null）
                bool scriptFound = (runResult is bool found) ? found : (runResult != null);

                if (!scriptFound)
                {
                    Console.Error.WriteLine($"[BRIDGE_ERROR] 脚本文件未找到或被 SpaceClaim 拒绝: {opts.ScriptPath}");
                    TryExitSpaceClaim(scApp, opts);
                    return 1;
                }
            }
            catch (Exception ex)
            {
                stopwatch.Stop();
                Console.Error.WriteLine($"[BRIDGE_ERROR] RunScript 调用异常: {ex.GetType().Name}: {ex.Message}");

                // COM RPC 断开：通常表示脚本内部调用了 Exit（脚本正常完成）
                if (ex is COMException && (ex.Message.Contains("RPC") || ex.Message.Contains("disconnected")))
                {
                    Console.WriteLine($"[BRIDGE] SpaceClaim COM 已断开 (脚本可能已正常退出 SpaceClaim)");
                    // 不返回错误 — 继续验证输出文件
                }
                else if (ex.Message.Contains("timeout") || ex.Message.Contains("超时") ||
                    stopwatch.Elapsed.TotalSeconds > opts.TimeoutSeconds)
                {
                    TryExitSpaceClaim(scApp, opts);
                    return 5;
                }
                else
                {
                    // 其他异常：尝试退出 SC 并返回失败
                    TryExitSpaceClaim(scApp, opts);
                    return 1;
                }
            }

            // 阶段4: 验证输出文件
            var scdocFile = Path.Combine(opts.ScdocDir, $"model_gen4_{opts.ConfigName}.scdoc");
            if (File.Exists(scdocFile))
            {
                var fileInfo = new FileInfo(scdocFile);
                Console.WriteLine($"[BRIDGE] ✓ SCDOC 输出文件已生成: {scdocFile} ({fileInfo.Length} bytes)");

                TryExitSpaceClaim(scApp, opts);
                return 0;
            }
            else
            {
                Console.Error.WriteLine($"[BRIDGE_ERROR] SCDOC 输出文件未生成: {scdocFile}");
                Console.Error.WriteLine("[BRIDGE_ERROR] 脚本可能执行失败但 RunScript 未报告错误");

                TryExitSpaceClaim(scApp, opts);
                return 3;
            }
        }

        /// <summary>
        /// 连接到 SpaceClaim 应用程序实例。
        ///
        /// 策略：
        ///   1. 首先尝试连接到已在运行的 SpaceClaim 实例
        ///   2. 如果未运行，启动新实例并等待其 COM 接口就绪
        ///   3. 尝试多个 COM ProgID（版本兼容性）
        /// </summary>
        private static object ConnectToSpaceClaim(BridgeOptions opts)
        {
            // 策略1: 尝试连接已有实例
            foreach (var progId in SpaceClaimProgIds)
            {
                try
                {
                    object app = Marshal.GetActiveObject(progId);
                    if (app != null)
                    {
                        Console.WriteLine($"[BRIDGE] 已连接到运行中的 SpaceClaim 实例 (ProgID={progId})");
                        return app;
                    }
                }
                catch (COMException)
                {
                    continue;
                }
            }

            // 策略2: 启动新实例
            string scExe = ResolveScExePath(opts);
            if (string.IsNullOrEmpty(scExe) || !File.Exists(scExe))
            {
                Console.Error.WriteLine($"[BRIDGE_ERROR] SpaceClaim 可执行文件未找到: {scExe ?? "(null)"}");
                return null;
            }

            Console.WriteLine($"[BRIDGE] 正在启动 SpaceClaim: {scExe}");
            try
            {
                var startInfo = new ProcessStartInfo
                {
                    FileName = scExe,
                    UseShellExecute = true,
                    WindowStyle = opts.Headless ? ProcessWindowStyle.Minimized : ProcessWindowStyle.Normal,
                };
                Process.Start(startInfo);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[BRIDGE_ERROR] 启动 SpaceClaim 失败: {ex.Message}");
                return null;
            }

            // 等待 SpaceClaim COM 接口就绪（最多等待 120 秒）
            Console.WriteLine("[BRIDGE] 等待 SpaceClaim COM 接口就绪...");
            int maxWaitSeconds = 120;
            for (int i = 0; i < maxWaitSeconds; i++)
            {
                Thread.Sleep(1000);

                foreach (var progId in SpaceClaimProgIds)
                {
                    try
                    {
                        object app = Marshal.GetActiveObject(progId);
                        if (app != null)
                        {
                            Console.WriteLine($"[BRIDGE] ✓ SpaceClaim COM 接口已就绪 (等待了 {i + 1}s, ProgID={progId})");
                            // 额外等待确保 GUI 完全初始化
                            Thread.Sleep(3000);
                            return app;
                        }
                    }
                    catch (COMException)
                    {
                        continue;
                    }
                }
            }

            Console.Error.WriteLine($"[BRIDGE_ERROR] SpaceClaim COM 接口等待超时 ({maxWaitSeconds}s)");
            return null;
        }

        /// <summary>
        /// 解析 SpaceClaim.exe 路径
        /// </summary>
        private static string ResolveScExePath(BridgeOptions opts)
        {
            // 优先使用命令行指定的路径
            if (!string.IsNullOrEmpty(opts.ScExePath) && File.Exists(opts.ScExePath))
            {
                return opts.ScExePath;
            }

            // 默认安装路径列表
            var candidatePaths = new[]
            {
                @"C:\Program Files\ANSYS Inc\v231\SCDM\SpaceClaim.exe",
                @"C:\Program Files\ANSYS Inc\v232\SCDM\SpaceClaim.exe",
                @"C:\Program Files\ANSYS Inc\v241\SCDM\SpaceClaim.exe",
            };

            foreach (var path in candidatePaths)
            {
                if (File.Exists(path))
                {
                    return path;
                }
            }

            // 所有候选路径均不存在
            return null;
        }

        /// <summary>
        /// 尝试退出 SpaceClaim（如果是由 Bridge 启动的）。
        /// 注意：不强制 kill 进程，仅发送正常退出命令。
        /// </summary>
        private static void TryExitSpaceClaim(dynamic scApp, BridgeOptions opts)
        {
            try
            {
                // 发送 Exit 命令给 SpaceClaim
                scApp.Command.Execute("Exit");
                Console.WriteLine("[BRIDGE] 已发送 SpaceClaim 退出命令");
            }
            catch (Exception)
            {
                // SpaceClaim 可能已经退出或 COM 代理已失效
                Console.WriteLine("[BRIDGE] SpaceClaim 退出命令发送失败（可能已退出）");
            }
        }
    }

    /// <summary>
    /// 命令行选项数据类
    /// </summary>
    internal class BridgeOptions
    {
        public string ScriptPath { get; set; }
        public string ConfigName { get; set; }
        public string StepDir { get; set; }
        public string ScdocDir { get; set; }
        public int TimeoutSeconds { get; set; } = 300;
        public string ScExePath { get; set; }
        public bool Headless { get; set; } = false;
    }
}
