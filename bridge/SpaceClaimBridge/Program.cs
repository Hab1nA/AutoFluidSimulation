using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Threading;

namespace AutoFluidSimulation.Bridge
{
    /// <summary>
    /// Bridge 退出码。
    /// </summary>
    internal enum ExitCode
    {
        Success = 0,
        ScriptFailed = 1,
        LaunchFailed = 2,
        OutputValidationFailed = 3,
        InvalidArgs = 4,
        Timeout = 5,
    }

    /// <summary>
    /// SpaceClaim Bridge — C# 中间衔接程序，纯进程检测模式
    ///
    /// SpaceClaim 不向外部暴露 out-of-process COM 自动化接口（与 AutoCAD/SolidWorks 不同），
    /// 因此采用命令行 /RunScript + 进程检测的纯进程模式。
    ///
    /// 用法：
    ///   SpaceClaimBridge.exe
    ///     --script   &lt;transit脚本路径&gt;
    ///     --config   &lt;构型编号&gt;
    ///     --stepdir  &lt;STEP文件目录&gt;
    ///     --scdocdir &lt;SCDOC输出目录&gt;
    ///     [--timeout &lt;秒数, 默认300&gt;]
    ///
    /// 返回值：
    ///   0 = 成功
    ///   1 = 脚本执行失败
    ///   2 = SpaceClaim 启动失败
    ///   3 = 输出文件验证失败
    ///   4 = 参数错误
    ///   5 = 超时
    /// </summary>
    internal class Program
    {
        private static readonly string[] SpaceClaimExePaths =
        {
            @"C:\Program Files\ANSYS Inc\v231\SCDM\SpaceClaim.exe",
            @"C:\Program Files\ANSYS Inc\v232\SCDM\SpaceClaim.exe",
            @"C:\Program Files\ANSYS Inc\v241\SCDM\SpaceClaim.exe",
        };

        private const string ProcessName = "SpaceClaim";
        private const string StepFileNamePattern = "model_gen4.SLDPRT_{0}.step";
        private const string ScdocFileNamePattern = "model_gen4_{0}.scdoc";
        private const int DefaultProcessAppearTimeoutSeconds = 120;
        private const int DefaultGuiReadyTimeoutSeconds = 30;
        private const int DefaultGuiStableDelaySeconds = 15;
        private const int DefaultPersistentReadyTimeoutSeconds = 180;
        private const int ScdocPollIntervalMs = 2000;
        private const int ProcessExitGraceDelayMs = 2000;
        private const int PersistentReadyPollIntervalMs = 2000;
        private const int PersistentLoopPollIntervalMs = 1000;
        private const int PersistentMonitorHeartbeatSeconds = 30;
        private const int ProcessAppearPollIntervalMs = 1000;
        private const int ProcessCheckRetryDelayMs = 2000;
        private const int WaitForInputIdleTimeoutMs = 15000;
        private const int GuiFallbackStableDelayMs = 10000;
        private const int GuiFallbackFixedDelayMs = 20000;
        private const int MaxConsecutiveProcessCheckFailures = 5;

        /// <summary>
        /// 程序入口点。解析命令行参数并执行 SpaceClaim 脚本，返回对应退出码。
        /// </summary>
        /// <param name="args">命令行参数数组</param>
        /// <returns>退出码：0=成功, 1=脚本失败, 2=启动失败, 3=输出验证失败, 4=参数错误, 5=超时</returns>
        private static int Main(string[] args)
        {
            try
            {
                var options = ParseArguments(args);
                if (options == null)
                {
                    return (int)ExitCode.InvalidArgs;
                }

                return Execute(options);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[BRIDGE_FATAL] 未处理的异常: {ex}");
                return (int)ExitCode.ScriptFailed;
            }
        }

        /// <summary>
        /// 解析命令行参数并返回结构化选项。
        /// </summary>
        /// <param name="args">原始命令行参数数组</param>
        /// <returns>解析后的选项对象，解析失败返回 null</returns>
        private static BridgeOptions ParseArguments(string[] args)
        {
            var options = new BridgeOptions();

            for (int i = 0; i < args.Length; i++)
            {
                switch (args[i].ToLowerInvariant())
                {
                    case "--script":
                        options.ScriptPath = ReadRequiredArgumentValue(args, ref i, "--script");
                        if (options.ScriptPath == null) return null;
                        break;
                    case "--config":
                        options.ConfigName = ReadRequiredArgumentValue(args, ref i, "--config");
                        if (options.ConfigName == null) return null;
                        break;
                    case "--stepdir":
                        options.StepDir = ReadRequiredArgumentValue(args, ref i, "--stepdir");
                        if (options.StepDir == null) return null;
                        break;
                    case "--scdocdir":
                        options.ScdocDir = ReadRequiredArgumentValue(args, ref i, "--scdocdir");
                        if (options.ScdocDir == null) return null;
                        break;
                    case "--timeout":
                        string timeoutValue = ReadRequiredArgumentValue(args, ref i, "--timeout");
                        if (timeoutValue == null) return null;
                        if (!int.TryParse(timeoutValue, out int t) || t <= 0)
                        {
                            Console.Error.WriteLine("[BRIDGE_ERROR] 参数 --timeout 必须是正整数");
                            PrintUsage();
                            return null;
                        }
                        options.TimeoutSeconds = t;
                        break;
                    case "--sc-exe":
                        options.ScExePath = ReadRequiredArgumentValue(args, ref i, "--sc-exe");
                        if (options.ScExePath == null) return null;
                        break;
                    case "--persistent":
                        options.Persistent = true;
                        break;
                    case "--cmddir":
                        options.CmdDir = ReadRequiredArgumentValue(args, ref i, "--cmddir");
                        if (options.CmdDir == null) return null;
                        break;
                    case "--slotid":
                        string slotValue = ReadRequiredArgumentValue(args, ref i, "--slotid");
                        if (slotValue == null) return null;
                        if (!int.TryParse(slotValue, out int sid) || sid < 0)
                        {
                            Console.Error.WriteLine("[BRIDGE_ERROR] 参数 --slotid 必须是非负整数");
                            PrintUsage();
                            return null;
                        }
                        options.SlotId = sid;
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
                // 常驻模式只需要 Script 和 CmdDir
                if (options.Persistent)
                {
                    if (string.IsNullOrEmpty(options.ScriptPath) ||
                        string.IsNullOrEmpty(options.CmdDir))
                    {
                        Console.Error.WriteLine("[BRIDGE_ERROR] 常驻模式缺少必要参数 (--script, --cmddir)");
                        PrintUsage();
                        return null;
                    }
                }
                else
                {
                    Console.Error.WriteLine("[BRIDGE_ERROR] 缺少必要参数");
                    PrintUsage();
                    return null;
                }
            }

            return options;
        }

        /// <summary>
        /// 读取命令行参数中指定选项的值，索引自动前进。
        /// </summary>
        /// <param name="args">命令行参数数组</param>
        /// <param name="index">当前选项索引（引用传递，自动递增）</param>
        /// <param name="optionName">选项名称（用于错误消息）</param>
        /// <returns>参数值，缺少值时返回 null</returns>
        private static string ReadRequiredArgumentValue(string[] args, ref int index, string optionName)
        {
            if (index + 1 >= args.Length || args[index + 1].StartsWith("--", StringComparison.Ordinal))
            {
                Console.Error.WriteLine($"[BRIDGE_ERROR] 参数 {optionName} 缺少值");
                PrintUsage();
                return null;
            }

            index++;
            return args[index];
        }

        /// <summary>
        /// 输出命令行用法说明到 stderr。
        /// </summary>
        private static void PrintUsage()
        {
            Console.Error.WriteLine(@"
用法: SpaceClaimBridge.exe
  --script   <transit脚本路径>    (必需)
  --config   <构型编号>           (必需)
  --stepdir  <STEP文件目录>       (必需)
  --scdocdir <SCDOC输出目录>      (必需)
  [--timeout <秒数>]              (可选, 默认300)
  [--sc-exe  <SpaceClaim.exe路径>] (可选, 覆盖自动检测)

常驻模式:
  --persistent                    (启用常驻模式)
  --script   <transit脚本路径>    (必需)
  --cmddir   <IPC命令目录>       (必需)
  --slotid   <槽位ID>            (可选, 默认0)
");
        }

        /// <summary>
        /// 根据模式（一次性/常驻）执行 SpaceClaim 脚本。
        /// </summary>
        /// <param name="opts">解析后的 Bridge 选项</param>
        /// <returns>退出码</returns>
        private static int Execute(BridgeOptions opts)
        {
            if (opts.Persistent)
            {
                return ExecutePersistent(opts);
            }

            Console.WriteLine($"[BRIDGE] SpaceClaim Bridge 启动");
            Console.WriteLine($"[BRIDGE] 脚本: {opts.ScriptPath}");
            Console.WriteLine($"[BRIDGE] 构型: {opts.ConfigName}");
            Console.WriteLine($"[BRIDGE] STEP目录: {opts.StepDir}");
            Console.WriteLine($"[BRIDGE] SCDOC目录: {opts.ScdocDir}");
            Console.WriteLine($"[BRIDGE] 超时: {opts.TimeoutSeconds}s");

            var stepFile = GetStepFilePath(opts);
            if (!File.Exists(stepFile))
            {
                Console.Error.WriteLine($"[BRIDGE_ERROR] STEP 文件不存在: {stepFile}");
                return (int)ExitCode.OutputValidationFailed;
            }
            Console.WriteLine($"[BRIDGE] STEP文件: {stepFile} ({new FileInfo(stepFile).Length} bytes)");

            Directory.CreateDirectory(opts.ScdocDir);

            string scExe = FindSpaceClaimExe(opts.ScExePath);
            if (scExe == null)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim.exe 未找到");
                return (int)ExitCode.LaunchFailed;
            }

            DateTime launchBaseline = DateTime.UtcNow;

            string runScriptArg = $"/RunScript=\"{opts.ScriptPath}\"";
            int processAppearTimeout = GetEnvInt(
                "AUTOFLUID_SC_PROCESS_APPEAR_TIMEOUT",
                DefaultProcessAppearTimeoutSeconds);
            Process workingProcess = null;

            Console.WriteLine("[BRIDGE] 正在启动 SpaceClaim (环境变量传参模式)...");
            Console.WriteLine($"[BRIDGE] Exe: {scExe}");
            Console.WriteLine($"[BRIDGE] Args: {runScriptArg} /Splash=False /Welcome=False /ExitAfterScript=True");
            Console.WriteLine($"[BRIDGE] Env: AUTOFLUID_SC_CONFIG={opts.ConfigName}");
            Console.WriteLine($"[BRIDGE] Env: AUTOFLUID_SC_STEP_DIR={opts.StepDir}");
            Console.WriteLine($"[BRIDGE] Env: AUTOFLUID_SC_SCDOC_DIR={opts.ScdocDir}");

            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = scExe,
                    Arguments = runScriptArg + " /Splash=False /Welcome=False /ExitAfterScript=True",
                    UseShellExecute = false,
                };
                psi.EnvironmentVariables["AUTOFLUID_SC_CONFIG"] = opts.ConfigName;
                psi.EnvironmentVariables["AUTOFLUID_SC_STEP_DIR"] = opts.StepDir;
                psi.EnvironmentVariables["AUTOFLUID_SC_SCDOC_DIR"] = opts.ScdocDir;
                Process started = Process.Start(psi);
                workingProcess = ResolveStartedSpaceClaimProcess(
                    started, launchBaseline, processAppearTimeout);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[BRIDGE_ERROR] 启动 SpaceClaim 失败: {ex.Message}");
                return (int)ExitCode.LaunchFailed;
            }

            if (workingProcess == null)
            {
                Console.Error.WriteLine($"[BRIDGE_ERROR] SpaceClaim 进程在 {processAppearTimeout}s 内未出现");
                return (int)ExitCode.LaunchFailed;
            }

            try
            {
                Console.WriteLine($"[BRIDGE] SpaceClaim 进程已出现 (PID={workingProcess.Id}), 等待 GUI 就绪...");
                int guiReadyTimeout = GetEnvInt(
                    "AUTOFLUID_SC_GUI_READY_TIMEOUT",
                    DefaultGuiReadyTimeoutSeconds);
                WaitForGuiReady(workingProcess, guiReadyTimeout);

                Console.WriteLine("[BRIDGE] SpaceClaim 正在运行, 监控脚本执行完成...");

                int totalTimeout = opts.TimeoutSeconds;
                DateTime deadline = DateTime.UtcNow.AddSeconds(totalTimeout);
                string scdocFile = GetScdocFilePath(opts);

                while (DateTime.UtcNow < deadline)
                {
                    if (File.Exists(scdocFile))
                    {
                        var fi = new FileInfo(scdocFile);
                        Console.WriteLine($"[BRIDGE] ✓ SCDOC 已生成: {scdocFile} ({fi.Length} bytes)");
                        return (int)ExitCode.Success;
                    }

                    bool processAlive = false;
                    try
                    {
                        if (workingProcess != null)
                        {
                            workingProcess.Refresh();
                            processAlive = !workingProcess.HasExited;
                        }
                    }
                    catch (Exception ex)
                    {
                        Console.Error.WriteLine($"[BRIDGE] Warning: 进程状态检查失败: {ex.Message}");
                        processAlive = false;
                    }

                    if (!processAlive)
                    {
                        Console.WriteLine("[BRIDGE] SpaceClaim 进程已退出, 最终检查...");
                        Thread.Sleep(ProcessExitGraceDelayMs);
                        if (File.Exists(scdocFile))
                        {
                            var fi2 = new FileInfo(scdocFile);
                            Console.WriteLine($"[BRIDGE] ✓ SCDOC 已生成: {scdocFile} ({fi2.Length} bytes)");
                            return (int)ExitCode.Success;
                        }
                        Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim 已退出但未生成 SCDOC 文件");
                        return (int)ExitCode.OutputValidationFailed;
                    }

                    Thread.Sleep(ScdocPollIntervalMs);
                }

                Console.Error.WriteLine($"[BRIDGE_ERROR] 超时 ({totalTimeout}s)");
                return (int)ExitCode.Timeout;
            }
            catch (InvalidOperationException ex)
            {
                Console.Error.WriteLine($"[BRIDGE_ERROR] SpaceClaim GUI 就绪检测失败 (进程已不可用): {ex.Message}");
                return (int)ExitCode.LaunchFailed;
            }
            finally
            {
                workingProcess?.Dispose();
            }
        }

        // ==============================================================
        // 常驻模式：启动 SpaceClaim 后通过文件协议循环处理命令
        // ==============================================================

        /// <summary>
        /// 常驻模式执行入口：启动 SpaceClaim 后通过文件协议循环处理命令。
        /// </summary>
        /// <param name="opts">解析后的 Bridge 选项</param>
        /// <returns>退出码</returns>
        private static int ExecutePersistent(BridgeOptions opts)
        {
            Console.WriteLine($"[BRIDGE] SpaceClaim Bridge 常驻模式启动");
            Console.WriteLine($"[BRIDGE] 脚本: {opts.ScriptPath}");
            Console.WriteLine($"[BRIDGE] 槽位: {opts.SlotId}");
            Console.WriteLine($"[BRIDGE] IPC目录: {opts.CmdDir}");

            if (string.IsNullOrEmpty(opts.CmdDir))
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] 常驻模式需要 --cmddir 参数");
                return (int)ExitCode.InvalidArgs;
            }

            Directory.CreateDirectory(opts.CmdDir);

            string scExe = FindSpaceClaimExe(opts.ScExePath);
            if (scExe == null)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim.exe 未找到");
                return (int)ExitCode.LaunchFailed;
            }

            DateTime launchBaseline = DateTime.UtcNow;
            string runScriptArg = $"/RunScript=\"{opts.ScriptPath}\"";
            int processAppearTimeout = GetEnvInt(
                "AUTOFLUID_SC_PROCESS_APPEAR_TIMEOUT",
                DefaultProcessAppearTimeoutSeconds);
            Process workingProcess = null;

            Console.WriteLine("[BRIDGE] 正在启动 SpaceClaim (常驻模式)...");
            Console.WriteLine($"[BRIDGE] Exe: {scExe}");
            Console.WriteLine($"[BRIDGE] Args: {runScriptArg} /Splash=False /Welcome=False");
            Console.WriteLine($"[BRIDGE] Env: AUTOFLUID_SC_PERSISTENT=1");
            Console.WriteLine($"[BRIDGE] Env: AUTOFLUID_SC_CMD_DIR={opts.CmdDir}");
            Console.WriteLine($"[BRIDGE] Env: AUTOFLUID_SC_SLOT_ID={opts.SlotId}");

            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = scExe,
                    // 常驻模式不使用 /ExitAfterScript=True
                    Arguments = runScriptArg + " /Splash=False /Welcome=False",
                    UseShellExecute = false,
                };
                // Python passes the same slot settings to Bridge; Bridge forwards them
                // to the child SpaceClaim process that runs spaceclaim_transit.py.
                psi.EnvironmentVariables["AUTOFLUID_SC_NOEXIT"] = "1";
                psi.EnvironmentVariables["AUTOFLUID_SC_PERSISTENT"] = "1";
                psi.EnvironmentVariables["AUTOFLUID_SC_CMD_DIR"] = opts.CmdDir;
                psi.EnvironmentVariables["AUTOFLUID_SC_SLOT_ID"] = opts.SlotId.ToString();
                Process started = Process.Start(psi);
                workingProcess = ResolveStartedSpaceClaimProcess(
                    started, launchBaseline, processAppearTimeout);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[BRIDGE_ERROR] 启动 SpaceClaim 失败: {ex.Message}");
                return (int)ExitCode.LaunchFailed;
            }

            if (workingProcess == null)
            {
                Console.Error.WriteLine($"[BRIDGE_ERROR] SpaceClaim 进程在 {processAppearTimeout}s 内未出现");
                return (int)ExitCode.LaunchFailed;
            }

            Console.WriteLine($"[BRIDGE] SpaceClaim 进程已出现 (PID={workingProcess.Id}), 等待 GUI 就绪...");
            WritePersistentMonitorFile(opts, workingProcess.Id, "launched");
            int guiReadyTimeout = GetEnvInt(
                "AUTOFLUID_SC_GUI_READY_TIMEOUT",
                DefaultGuiReadyTimeoutSeconds);
            try
            {
                WaitForGuiReady(workingProcess, guiReadyTimeout);
            }
            catch (InvalidOperationException ex)
            {
                Console.Error.WriteLine($"[BRIDGE_ERROR] SpaceClaim GUI 就绪检测失败 (进程已不可用): {ex.Message}");
                WritePersistentMonitorFile(opts, GetProcessIdOrDefault(workingProcess), "gui_ready_failed");
                workingProcess?.Dispose();
                return (int)ExitCode.LaunchFailed;
            }

            // 等待脚本就绪标志
            string readyFile = Path.Combine(opts.CmdDir, $"sc_ready_{opts.SlotId}.json");
            Console.WriteLine($"[BRIDGE] 等待脚本就绪标志: {readyFile}");
            int persistentReadyTimeout = GetEnvInt(
                "AUTOFLUID_SC_PERSISTENT_READY_TIMEOUT",
                DefaultPersistentReadyTimeoutSeconds);
            DateTime readyDeadline = DateTime.UtcNow.AddSeconds(persistentReadyTimeout);
            while (DateTime.UtcNow < readyDeadline)
            {
                if (File.Exists(readyFile))
                {
                    Console.WriteLine("[BRIDGE] 脚本就绪标志已检测到");
                    break;
                }
                try
                {
                    workingProcess.Refresh();
                    if (workingProcess.HasExited)
                    {
                        Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim 进程已退出，脚本未就绪");
                        return (int)ExitCode.LaunchFailed;
                    }
                }
                catch (InvalidOperationException)
                {
                    // 进程对象已释放 → 确认退出
                    Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim 进程对象已释放，脚本未就绪");
                    return (int)ExitCode.LaunchFailed;
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine($"[BRIDGE] Warning: 进程状态检查异常: {ex.Message}");
                }
                Thread.Sleep(PersistentReadyPollIntervalMs);
            }

            if (!File.Exists(readyFile))
            {
                Console.Error.WriteLine(
                    $"[BRIDGE_ERROR] 脚本就绪超时 ({persistentReadyTimeout}s)");
                return (int)ExitCode.Timeout;
            }

            Console.WriteLine("[BRIDGE] 常驻模式就绪，开始命令循环...");

            string cmdFile = Path.Combine(opts.CmdDir, $"sc_cmd_{opts.SlotId}.json");

            // 命令循环：监听进程退出和 quit 文件命令
            int exitCode = (int)ExitCode.Success;
            bool quitRequested = false;
            DateTime lastMonitorUpdate = DateTime.UtcNow;
            // ★ 进程检测连续失败计数器：防止因瞬态异常（如进程句柄暂不可用）
            //    误判 SpaceClaim 退出。累计 5 次连续失败才确认退出。
            int consecutiveProcessCheckFailures = 0;

            try
            {
                while (true)
                {
                    // ★ 检测 SpaceClaim 进程是否已退出（容错增强版）
                    //    单次 Refresh/HasExited 可能因瞬态异常（Win32Exception、
                    //    InvalidOperationException）失败，不能直接判定退出。
                    //    连续 N 次失败后才确认进程确实已退出。
                    try
                    {
                        workingProcess.Refresh();
                        if (workingProcess.HasExited)
                        {
                            bool quitPending = quitRequested || IsQuitCommandPending(cmdFile);
                            if (quitPending)
                            {
                                Console.WriteLine("[BRIDGE] SpaceClaim 已按 quit 请求退出，Bridge 退出");
                                exitCode = (int)ExitCode.Success;
                            }
                            else
                            {
                                Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim 进程异常退出，Bridge 退出");
                                exitCode = (int)ExitCode.ScriptFailed;
                            }
                            WritePersistentMonitorFile(
                                opts,
                                GetProcessIdOrDefault(workingProcess),
                                "spaceclaim_exited");
                            break;
                        }
                        // 检测成功 → 重置失败计数器
                        consecutiveProcessCheckFailures = 0;
                    }
                    catch (InvalidOperationException)
                    {
                        // 进程对象已释放 → 确认退出
                        bool quitPending = quitRequested || IsQuitCommandPending(cmdFile);
                        if (quitPending)
                        {
                            Console.WriteLine("[BRIDGE] SpaceClaim 进程对象已在 quit 请求后释放，Bridge 退出");
                            exitCode = (int)ExitCode.Success;
                        }
                        else
                        {
                            Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim 进程对象已释放，Bridge 退出");
                            exitCode = (int)ExitCode.ScriptFailed;
                        }
                        WritePersistentMonitorFile(
                            opts,
                            GetProcessIdOrDefault(workingProcess),
                            "process_handle_released");
                        break;
                    }
                    catch (Exception ex)
                    {
                        consecutiveProcessCheckFailures++;
                        Console.Error.WriteLine(
                            "[BRIDGE] Warning: 进程状态检查异常 "
                            + $"({consecutiveProcessCheckFailures}/{MaxConsecutiveProcessCheckFailures}): {ex.Message}");
                        if (consecutiveProcessCheckFailures >= MaxConsecutiveProcessCheckFailures)
                        {
                            Console.Error.WriteLine(
                                "[BRIDGE] 进程状态检查连续失败，判定 SpaceClaim 已退出");
                            WritePersistentMonitorFile(
                                opts,
                                GetProcessIdOrDefault(workingProcess),
                                "process_check_failed");
                            exitCode = (int)ExitCode.LaunchFailed;
                            break;
                        }
                        // 短暂等待后重试
                        Thread.Sleep(ProcessCheckRetryDelayMs);
                        continue;
                    }

                    if ((DateTime.UtcNow - lastMonitorUpdate).TotalSeconds >= PersistentMonitorHeartbeatSeconds)
                    {
                        WritePersistentMonitorFile(
                            opts,
                            GetProcessIdOrDefault(workingProcess),
                            "running");
                        lastMonitorUpdate = DateTime.UtcNow;
                    }

                    // ★ 检测 quit 命令文件（Python 端 shutdown 时写入）。
                    //    Bridge 只读不删，命令文件由 SpaceClaim transit 脚本消费。
                    if (!quitRequested && IsQuitCommandPending(cmdFile))
                    {
                        quitRequested = true;
                        Console.WriteLine("[BRIDGE] 收到 quit 命令，等待 SpaceClaim 脚本退出...");
                        WritePersistentMonitorFile(
                            opts,
                            GetProcessIdOrDefault(workingProcess),
                            "quit_requested");
                        lastMonitorUpdate = DateTime.UtcNow;
                    }

                    Thread.Sleep(PersistentLoopPollIntervalMs);
                }
            }
            finally
            {
                workingProcess?.Dispose();
            }

            Console.WriteLine("[BRIDGE] 常驻模式退出");
            return exitCode;
        }

        /// <summary>
        /// 解析 Process.Start 返回的句柄，不可用时回退到进程扫描。
        /// </summary>
        /// <param name="startedProcess">Process.Start 返回的进程对象</param>
        /// <param name="launchBaseline">启动时间基线</param>
        /// <param name="processAppearTimeout">进程出现超时秒数</param>
        /// <returns>可用的 SpaceClaim 进程对象，未找到返回 null</returns>
        private static Process ResolveStartedSpaceClaimProcess(
            Process startedProcess,
            DateTime launchBaseline,
            int processAppearTimeout)
        {
            if (startedProcess != null)
            {
                try
                {
                    startedProcess.Refresh();
                    if (!startedProcess.HasExited)
                    {
                        Console.WriteLine(
                            $"[BRIDGE] 使用 Process.Start 返回的 SpaceClaim 句柄 (PID={startedProcess.Id})");
                        return startedProcess;
                    }
                    Console.WriteLine("[BRIDGE] Process.Start 返回的进程已退出，回退到进程扫描");
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine(
                        $"[BRIDGE] Warning: 检查启动进程句柄失败，回退到进程扫描: {ex.Message}");
                }
            }

            Console.WriteLine("[BRIDGE] 等待 SpaceClaim 进程出现...");
            return WaitForProcessAppear(launchBaseline, processAppearTimeout);
        }

        /// <summary>
        /// 根据配置构建 STEP 文件的完整路径。
        /// </summary>
        /// <param name="opts">Bridge 选项</param>
        /// <returns>STEP 文件路径</returns>
        private static string GetStepFilePath(BridgeOptions opts)
        {
            return Path.Combine(
                opts.StepDir,
                string.Format(StepFileNamePattern, opts.ConfigName));
        }

        /// <summary>
        /// 根据配置构建 SCDOC 输出文件的完整路径。
        /// </summary>
        /// <param name="opts">Bridge 选项</param>
        /// <returns>SCDOC 文件路径</returns>
        private static string GetScdocFilePath(BridgeOptions opts)
        {
            return Path.Combine(
                opts.ScdocDir,
                string.Format(ScdocFileNamePattern, opts.ConfigName));
        }

        /// <summary>
        /// 写入常驻模式监控文件（JSON 格式），供 Python 端轮询读取。
        /// </summary>
        /// <param name="opts">Bridge 选项</param>
        /// <param name="spaceClaimPid">SpaceClaim 进程 PID</param>
        /// <param name="status">当前状态字符串</param>
        private static void WritePersistentMonitorFile(
            BridgeOptions opts,
            int spaceClaimPid,
            string status)
        {
            if (opts == null || string.IsNullOrEmpty(opts.CmdDir))
            {
                return;
            }

            try
            {
                string monitorFile = Path.Combine(opts.CmdDir, $"sc_bridge_{opts.SlotId}.json");
                int bridgePid = Process.GetCurrentProcess().Id;
                string json =
                    "{" +
                    $"\"slot_id\":{opts.SlotId}," +
                    $"\"bridge_pid\":{bridgePid}," +
                    $"\"spaceclaim_pid\":{spaceClaimPid}," +
                    $"\"status\":\"{EscapeJsonString(status)}\"," +
                    $"\"timestamp_utc\":\"{DateTime.UtcNow:O}\"" +
                    "}";
                File.WriteAllText(monitorFile, json);
                Console.WriteLine($"[BRIDGE] 监控信息已写入: {monitorFile}");
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[BRIDGE] Warning: 写入监控信息失败: {ex.Message}");
            }
        }

        /// <summary>
        /// 以共享模式读取命令文件内容，文件不存在或被锁定时返回 null。
        /// </summary>
        /// <param name="cmdFile">命令文件路径</param>
        /// <returns>文件内容，读取失败返回 null</returns>
        private static string ReadCommandFile(string cmdFile)
        {
            try
            {
                if (!File.Exists(cmdFile))
                {
                    return null;
                }

                using (var fs = new FileStream(
                    cmdFile,
                    FileMode.Open,
                    FileAccess.Read,
                    FileShare.ReadWrite | FileShare.Delete))
                using (var reader = new StreamReader(fs))
                {
                    return reader.ReadToEnd().Trim();
                }
            }
            catch (IOException)
            {
                return null;
            }
            catch (UnauthorizedAccessException)
            {
                return null;
            }
        }

        /// <summary>
        /// 检查命令文件中是否包含 quit 指令。
        /// </summary>
        /// <param name="cmdFile">命令文件路径</param>
        /// <returns>包含 quit 指令返回 true</returns>
        private static bool IsQuitCommandPending(string cmdFile)
        {
            string content = ReadCommandFile(cmdFile);
            return content != null && content.Contains("\"quit\"");
        }

        /// <summary>
        /// 安全获取进程 PID，进程对象不可用时返回 0。
        /// </summary>
        /// <param name="process">进程对象（可为 null）</param>
        /// <returns>进程 PID 或 0</returns>
        private static int GetProcessIdOrDefault(Process process)
        {
            if (process == null)
            {
                return 0;
            }

            try
            {
                return process.Id;
            }
            catch (InvalidOperationException)
            {
                return 0;
            }
        }

        /// <summary>
        /// 转义字符串中的 JSON 特殊字符和控制字符。
        /// </summary>
        /// <param name="value">待转义的原始字符串</param>
        /// <returns>转义后的 JSON 安全字符串</returns>
        private static string EscapeJsonString(string value)
        {
            if (value == null)
            {
                return string.Empty;
            }

            var sb = new System.Text.StringBuilder(value.Length + 16);
            foreach (char c in value)
            {
                switch (c)
                {
                    case '\\': sb.Append("\\\\"); break;
                    case '"': sb.Append("\\\""); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20)
                        {
                            sb.Append("\\u");
                            sb.Append(((int)c).ToString("x4"));
                        }
                        else
                        {
                            sb.Append(c);
                        }
                        break;
                }
            }
            return sb.ToString();
        }

        /// <summary>
        /// 按优先级查找 SpaceClaim.exe：命令行指定 → 环境变量 → 已知安装路径。
        /// </summary>
        /// <param name="userPath">命令行指定的可选路径</param>
        /// <returns>找到的可执行文件路径，未找到返回 null</returns>
        private static string FindSpaceClaimExe(string userPath = null)
        {
            // 1. 优先使用命令行指定的路径
            if (!string.IsNullOrEmpty(userPath) && File.Exists(userPath))
            {
                Console.WriteLine($"[BRIDGE] 找到 SpaceClaim.exe (命令行): {userPath}");
                return userPath;
            }
            // 2. 检查环境变量 AUTOFLUID_SC_EXE
            string envPath = Environment.GetEnvironmentVariable("AUTOFLUID_SC_EXE");
            if (!string.IsNullOrEmpty(envPath) && File.Exists(envPath))
            {
                Console.WriteLine($"[BRIDGE] 找到 SpaceClaim.exe (环境变量): {envPath}");
                return envPath;
            }
            // 3. 自动检测已知安装路径
            foreach (string p in SpaceClaimExePaths)
            {
                if (File.Exists(p))
                {
                    Console.WriteLine($"[BRIDGE] 找到 SpaceClaim.exe: {p}");
                    return p;
                }
            }

            Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim.exe 未找到, 已检查:");
            if (!string.IsNullOrEmpty(userPath))
            {
                Console.Error.WriteLine($"  命令行路径: {userPath}");
            }
            if (!string.IsNullOrEmpty(envPath))
            {
                Console.Error.WriteLine($"  环境变量路径: {envPath}");
            }
            foreach (string p in SpaceClaimExePaths)
            {
                Console.Error.WriteLine($"  已知路径: {p}");
            }
            return null;
        }

        /// <summary>
        /// 等待 SpaceClaim 进程出现，过滤启动前已有的旧进程。
        /// </summary>
        /// <param name="after">启动时间基线（UTC）</param>
        /// <param name="timeoutSec">等待超时秒数</param>
        /// <returns>匹配的进程对象，超时返回 null</returns>
        private static Process WaitForProcessAppear(DateTime after, int timeoutSec)
        {
            // 记录启动前已有的进程 PID，用于过滤旧进程
            var existingPids = new HashSet<int>();
            foreach (var p in Process.GetProcessesByName(ProcessName))
            {
                try { existingPids.Add(p.Id); }
                catch (InvalidOperationException) { /* 进程已退出，跳过 */ }
                p.Dispose();
            }

            DateTime deadline = DateTime.UtcNow.AddSeconds(timeoutSec);
            while (DateTime.UtcNow < deadline)
            {
                Process[] procs = Process.GetProcessesByName(ProcessName);
                Process selectedProcess = null;
                try
                {
                    foreach (Process p in procs)
                    {
                        try
                        {
                            // 优先匹配启动后的新进程（不在旧 PID 集合中）
                            if (!existingPids.Contains(p.Id) && p.StartTime.ToUniversalTime() >= after)
                            {
                                selectedProcess = p;
                                return p;
                            }
                        }
                        catch (Exception ex)
                        {
                            // 无权访问进程 StartTime（如跨会话进程），跳过
                            Console.Error.WriteLine($"[BRIDGE] Warning: 访问进程信息失败 (PID={p.Id}): {ex.Message}");
                        }
                    }

                    // fallback：如果有新进程但无法获取 StartTime，返回第一个非旧进程
                    foreach (Process p in procs)
                    {
                        try
                        {
                            if (!existingPids.Contains(p.Id))
                            {
                                selectedProcess = p;
                                return p;
                            }
                        }
                        catch (Exception ex)
                        {
                            Console.Error.WriteLine($"[BRIDGE] Warning: 返回进程失败: {ex.Message}");
                        }
                    }
                }
                finally
                {
                    if (selectedProcess == null)
                    {
                        DisposeProcesses(procs);
                    }
                    else
                    {
                        DisposeUnmatchedProcesses(procs, selectedProcess);
                    }
                }

                Thread.Sleep(ProcessAppearPollIntervalMs);
            }
            return null;
        }

        /// <summary>
        /// 释放除已选中进程外的所有进程对象。
        /// </summary>
        /// <param name="processes">待清理的进程集合</param>
        /// <param name="selected">保留不释放的进程</param>
        private static void DisposeUnmatchedProcesses(IEnumerable<Process> processes, Process selected)
        {
            foreach (Process process in processes)
            {
                if (!object.ReferenceEquals(process, selected))
                {
                    try
                    {
                        process.Dispose();
                    }
                    catch (Exception)
                    {
                        // Dispose 期间的任何异常均忽略，确保批量清理不中断
                    }
                }
            }
        }

        /// <summary>
        /// 释放所有进程对象，忽略已释放的对象。
        /// </summary>
        /// <param name="processes">待清理的进程集合</param>
        private static void DisposeProcesses(IEnumerable<Process> processes)
        {
            foreach (Process process in processes)
            {
                try
                {
                    process.Dispose();
                }
                catch (Exception)
                {
                    // Dispose 期间的任何异常均忽略，确保批量清理不中断
                }
            }
        }

        /// <summary>
        /// 从环境变量读取正整数值，无效或未设置时返回默认值。
        /// </summary>
        /// <param name="name">环境变量名</param>
        /// <param name="defaultValue">默认值</param>
        /// <returns>解析后的正整数或默认值</returns>
        private static int GetEnvInt(string name, int defaultValue)
        {
            string value = Environment.GetEnvironmentVariable(name);
            if (!string.IsNullOrEmpty(value) && int.TryParse(value, out int parsed) && parsed > 0)
            {
                return parsed;
            }
            return defaultValue;
        }

        /// <summary>
        /// 等待 SpaceClaim GUI 就绪：主窗口出现 → 线程空闲 → 加载稳定延时。
        /// </summary>
        /// <param name="p">SpaceClaim 进程对象</param>
        /// <param name="timeoutSec">主窗口出现超时秒数</param>
        private static void WaitForGuiReady(Process p, int timeoutSec)
        {
            DateTime deadline = DateTime.UtcNow.AddSeconds(timeoutSec);

            Console.WriteLine("[BRIDGE] Phase 1: 等待 SpaceClaim 主窗口出现...");
            bool mainWindowFound = false;
            while (DateTime.UtcNow < deadline && !mainWindowFound)
            {
                try
                {
                    p.Refresh();
                    if (p.MainWindowHandle != IntPtr.Zero)
                    {
                        string title = p.MainWindowTitle;
                        if (!string.IsNullOrEmpty(title))
                        {
                            Console.WriteLine($"[BRIDGE] 主窗口已检测到: \"{title}\"");
                            mainWindowFound = true;
                        }
                    }
                }
                catch (InvalidOperationException ex)
                {
                    Console.Error.WriteLine($"[BRIDGE_ERROR] SpaceClaim 进程已不可用: {ex.Message}");
                    throw;
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine($"[BRIDGE] Warning: 主窗口检测异常: {ex.Message}");
                }
                if (!mainWindowFound) Thread.Sleep(ProcessAppearPollIntervalMs);
            }

            if (!mainWindowFound)
            {
                Console.WriteLine("[BRIDGE] Phase 1 超时, 使用 WaitForInputIdle 兜底...");
                try
                {
                    if (p.WaitForInputIdle(WaitForInputIdleTimeoutMs))
                    {
                        Console.WriteLine("[BRIDGE] WaitForInputIdle 兜底 OK");
                        Thread.Sleep(GuiFallbackStableDelayMs);
                        return;
                    }
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine($"[BRIDGE] Warning: WaitForInputIdle 异常: {ex.Message}");
                }
                Console.WriteLine(
                    $"[BRIDGE] 兜底失败, 使用固定延时 ({GuiFallbackFixedDelayMs / 1000}s)");
                Thread.Sleep(GuiFallbackFixedDelayMs);
                return;
            }

            Console.WriteLine("[BRIDGE] Phase 2: 等待主窗口线程空闲...");
            try
            {
                p.Refresh();
                if (p.WaitForInputIdle(WaitForInputIdleTimeoutMs))
                {
                    Console.WriteLine("[BRIDGE] 主窗口线程已空闲");
                }
                else
                {
                    Console.WriteLine("[BRIDGE] WaitForInputIdle 超时, 继续...");
                }
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[BRIDGE] Warning: Phase 2 WaitForInputIdle 异常: {ex.Message}");
            }

            // Phase 3 等待时间可通过环境变量配置（默认 15 秒）
            int guiWaitSeconds = GetEnvInt(
                "AUTOFLUID_SC_GUI_STABLE_DELAY",
                DefaultGuiStableDelaySeconds);
            Console.WriteLine($"[BRIDGE] Phase 3: 等待加载稳定 (延时 {guiWaitSeconds}s)...");
            Thread.Sleep(guiWaitSeconds * 1000);
            Console.WriteLine("[BRIDGE] SpaceClaim GUI 加载完成");
        }
    }

    /// <summary>
    /// SpaceClaim Bridge 命令行参数选项。
    /// </summary>
    internal class BridgeOptions
    {
        /// <summary>SpaceClaim transit 脚本路径（必需）。</summary>
        public string ScriptPath { get; set; }

        /// <summary>构型编号，用于构建 STEP/SCDOC 文件名（一次性模式必需）。</summary>
        public string ConfigName { get; set; }

        /// <summary>STEP 文件所在目录（一次性模式必需）。</summary>
        public string StepDir { get; set; }

        /// <summary>SCDOC 输出目录（一次性模式必需）。</summary>
        public string ScdocDir { get; set; }

        /// <summary>SpaceClaim 进程超时秒数，默认 300。</summary>
        public int TimeoutSeconds { get; set; } = 300;

        /// <summary>自定义 SpaceClaim.exe 路径，覆盖自动检测。</summary>
        public string ScExePath { get; set; }

        /// <summary>是否启用常驻模式（循环处理命令）。</summary>
        public bool Persistent { get; set; }

        /// <summary>常驻模式 IPC 命令目录路径（常驻模式必需）。</summary>
        public string CmdDir { get; set; }

        /// <summary>常驻模式槽位 ID，默认 0。</summary>
        public int SlotId { get; set; }
    }
}
