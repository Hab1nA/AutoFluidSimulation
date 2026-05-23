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
    enum ExitCode
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
    class Program
    {
        private static readonly string[] SpaceClaimExePaths =
        {
            @"C:\Program Files\ANSYS Inc\v231\SCDM\SpaceClaim.exe",
            @"C:\Program Files\ANSYS Inc\v232\SCDM\SpaceClaim.exe",
            @"C:\Program Files\ANSYS Inc\v241\SCDM\SpaceClaim.exe",
        };

        private const string ProcessName = "SpaceClaim";

        static int Main(string[] args)
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
                    case "--persistent":
                        options.Persistent = true;
                        break;
                    case "--cmddir":
                        if (++i < args.Length) options.CmdDir = args[i];
                        break;
                    case "--slotid":
                        if (++i < args.Length && int.TryParse(args[i], out int sid))
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

        private static int Execute(BridgeOptions opts)
        {
            if (opts.Persistent)
            {
                return ExecutePersistent(opts);
            }

            Console.WriteLine($"[BRIDGE] SpaceClaim Bridge 启动");
            Console.WriteLine($"[BRIDGE]   脚本: {opts.ScriptPath}");
            Console.WriteLine($"[BRIDGE]   构型: {opts.ConfigName}");
            Console.WriteLine($"[BRIDGE]   STEP目录: {opts.StepDir}");
            Console.WriteLine($"[BRIDGE]   SCDOC目录: {opts.ScdocDir}");
            Console.WriteLine($"[BRIDGE]   超时: {opts.TimeoutSeconds}s");

            var stepFile = Path.Combine(opts.StepDir, $"model_gen4.SLDPRT_{opts.ConfigName}.step");
            if (!File.Exists(stepFile))
            {
                Console.Error.WriteLine($"[BRIDGE_ERROR] STEP 文件不存在: {stepFile}");
                return (int)ExitCode.OutputValidationFailed;
            }
            Console.WriteLine($"[BRIDGE]   STEP文件: {stepFile} ({new FileInfo(stepFile).Length} bytes)");

            Directory.CreateDirectory(opts.ScdocDir);

            string scExe = FindSpaceClaimExe(opts.ScExePath);
            if (scExe == null)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim.exe 未找到");
                return (int)ExitCode.LaunchFailed;
            }

            DateTime launchBaseline = DateTime.UtcNow;

            string runScriptArg = $"/RunScript=\"{opts.ScriptPath}\"";

            Console.WriteLine("[BRIDGE] 正在启动 SpaceClaim (环境变量传参模式)...");
            Console.WriteLine($"[BRIDGE]   Exe: {scExe}");
            Console.WriteLine($"[BRIDGE]   Args: {runScriptArg} /Splash=False /Welcome=False /ExitAfterScript=True");
            Console.WriteLine($"[BRIDGE]   Env: AUTOFLUID_SC_CONFIG={opts.ConfigName}");
            Console.WriteLine($"[BRIDGE]   Env: AUTOFLUID_SC_STEP_DIR={opts.StepDir}");
            Console.WriteLine($"[BRIDGE]   Env: AUTOFLUID_SC_SCDOC_DIR={opts.ScdocDir}");

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
                using (var started = Process.Start(psi))
                {
                    // 仅用于触发启动，句柄由 WaitForProcessAppear 重新获取
                }
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[BRIDGE_ERROR] 启动 SpaceClaim 失败: {ex.Message}");
                return (int)ExitCode.LaunchFailed;
            }

            Console.WriteLine("[BRIDGE] 等待 SpaceClaim 进程出现...");
            int processAppearTimeout = GetEnvInt("AUTOFLUID_SC_PROCESS_APPEAR_TIMEOUT", 120);
            Process workingProcess = WaitForProcessAppear(launchBaseline, processAppearTimeout);
            if (workingProcess == null)
            {
                Console.Error.WriteLine($"[BRIDGE_ERROR] SpaceClaim 进程在 {processAppearTimeout}s 内未出现");
                return (int)ExitCode.LaunchFailed;
            }

            try
            {
                Console.WriteLine($"[BRIDGE] SpaceClaim 进程已出现 (PID={workingProcess.Id}), 等待 GUI 就绪...");
                int guiReadyTimeout = GetEnvInt("AUTOFLUID_SC_GUI_READY_TIMEOUT", 30);
                WaitForGuiReady(workingProcess, guiReadyTimeout);

                Console.WriteLine("[BRIDGE] SpaceClaim 正在运行, 监控脚本执行完成...");

                int totalTimeout = opts.TimeoutSeconds;
                int pollIntervalMs = 2000;
                DateTime deadline = DateTime.UtcNow.AddSeconds(totalTimeout);
                string scdocFile = Path.Combine(opts.ScdocDir, $"model_gen4_{opts.ConfigName}.scdoc");

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
                        Thread.Sleep(2000);
                        if (File.Exists(scdocFile))
                        {
                            var fi2 = new FileInfo(scdocFile);
                            Console.WriteLine($"[BRIDGE] ✓ SCDOC 已生成: {scdocFile} ({fi2.Length} bytes)");
                            return (int)ExitCode.Success;
                        }
                        Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim 已退出但未生成 SCDOC 文件");
                        return (int)ExitCode.OutputValidationFailed;
                    }

                    Thread.Sleep(pollIntervalMs);
                }

                Console.Error.WriteLine($"[BRIDGE_ERROR] 超时 ({totalTimeout}s)");
                return (int)ExitCode.Timeout;
            }
            finally
            {
                workingProcess?.Dispose();
            }
        }

        // ==============================================================
        // 常驻模式：启动 SpaceClaim 后通过文件协议循环处理命令
        // ==============================================================
        private static int ExecutePersistent(BridgeOptions opts)
        {
            Console.WriteLine($"[BRIDGE] SpaceClaim Bridge 常驻模式启动");
            Console.WriteLine($"[BRIDGE]   脚本: {opts.ScriptPath}");
            Console.WriteLine($"[BRIDGE]   槽位: {opts.SlotId}");
            Console.WriteLine($"[BRIDGE]   IPC目录: {opts.CmdDir}");

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

            Console.WriteLine("[BRIDGE] 正在启动 SpaceClaim (常驻模式)...");
            Console.WriteLine($"[BRIDGE]   Exe: {scExe}");
            Console.WriteLine($"[BRIDGE]   Args: {runScriptArg} /Splash=False /Welcome=False");
            Console.WriteLine($"[BRIDGE]   Env: AUTOFLUID_SC_PERSISTENT=1");
            Console.WriteLine($"[BRIDGE]   Env: AUTOFLUID_SC_CMD_DIR={opts.CmdDir}");
            Console.WriteLine($"[BRIDGE]   Env: AUTOFLUID_SC_SLOT_ID={opts.SlotId}");

            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = scExe,
                    // 常驻模式不使用 /ExitAfterScript=True
                    Arguments = runScriptArg + " /Splash=False /Welcome=False",
                    UseShellExecute = false,
                };
                psi.EnvironmentVariables["AUTOFLUID_SC_NOEXIT"] = "1";
                psi.EnvironmentVariables["AUTOFLUID_SC_PERSISTENT"] = "1";
                psi.EnvironmentVariables["AUTOFLUID_SC_CMD_DIR"] = opts.CmdDir;
                psi.EnvironmentVariables["AUTOFLUID_SC_SLOT_ID"] = opts.SlotId.ToString();
                using (var started = Process.Start(psi))
                {
                    // 仅用于触发启动，句柄由 WaitForProcessAppear 重新获取
                }
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[BRIDGE_ERROR] 启动 SpaceClaim 失败: {ex.Message}");
                return (int)ExitCode.LaunchFailed;
            }

            Console.WriteLine("[BRIDGE] 等待 SpaceClaim 进程出现...");
            int processAppearTimeout = GetEnvInt("AUTOFLUID_SC_PROCESS_APPEAR_TIMEOUT", 120);
            Process workingProcess = WaitForProcessAppear(launchBaseline, processAppearTimeout);
            if (workingProcess == null)
            {
                Console.Error.WriteLine($"[BRIDGE_ERROR] SpaceClaim 进程在 {processAppearTimeout}s 内未出现");
                return (int)ExitCode.LaunchFailed;
            }

            Console.WriteLine($"[BRIDGE] SpaceClaim 进程已出现 (PID={workingProcess.Id}), 等待 GUI 就绪...");
            int guiReadyTimeout = GetEnvInt("AUTOFLUID_SC_GUI_READY_TIMEOUT", 30);
            WaitForGuiReady(workingProcess, guiReadyTimeout);

            // 等待脚本就绪标志
            string readyFile = Path.Combine(opts.CmdDir, $"sc_ready_{opts.SlotId}.json");
            Console.WriteLine($"[BRIDGE] 等待脚本就绪标志: {readyFile}");
            DateTime readyDeadline = DateTime.UtcNow.AddSeconds(120);
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
                catch (Exception ex)
                {
                    Console.Error.WriteLine($"[BRIDGE] Warning: 进程状态检查异常: {ex.Message}");
                }
                Thread.Sleep(2000);
            }

            if (!File.Exists(readyFile))
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] 脚本就绪超时 (120s)");
                return (int)ExitCode.Timeout;
            }

            Console.WriteLine("[BRIDGE] 常驻模式就绪，开始命令循环...");

            string cmdFile = Path.Combine(opts.CmdDir, $"sc_cmd_{opts.SlotId}.json");

            // 命令循环：监听 quit 文件命令或进程退出
            int exitCode = (int)ExitCode.Success;
            try
            {
                while (true)
                {
                    // ★ 检测 quit 命令文件（Python 端 shutdown 时写入）
                    try
                    {
                        if (File.Exists(cmdFile))
                        {
                            string content = File.ReadAllText(cmdFile).Trim();
                            if (content.Contains("\"quit\""))
                            {
                                Console.WriteLine("[BRIDGE] 收到 quit 命令，正在退出...");
                                try { File.Delete(cmdFile); } catch { }
                                break;
                            }
                        }
                    }
                    catch (IOException)
                    {
                        // 文件可能正被 transit 脚本读取，忽略
                    }

                    // 检测 SpaceClaim 进程是否已退出
                    try
                    {
                        workingProcess.Refresh();
                        if (workingProcess.HasExited)
                        {
                            Console.WriteLine("[BRIDGE] SpaceClaim 进程已退出，Bridge 退出");
                            break;
                        }
                    }
                    catch
                    {
                        Console.WriteLine("[BRIDGE] SpaceClaim 进程状态检查异常，Bridge 退出");
                        break;
                    }

                    Thread.Sleep(1000);
                }
            }
            finally
            {
                workingProcess?.Dispose();
            }

            Console.WriteLine("[BRIDGE] 常驻模式退出");
            return exitCode;
        }

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
            return null;
        }

        private static Process WaitForProcessAppear(DateTime after, int timeoutSec)
        {
            // 记录启动前已有的进程 PID，用于过滤旧进程
            var existingPids = new HashSet<int>();
            foreach (var p in Process.GetProcessesByName(ProcessName))
            {
                try { existingPids.Add(p.Id); } catch { }
                p.Dispose();
            }

            DateTime deadline = DateTime.UtcNow.AddSeconds(timeoutSec);
            while (DateTime.UtcNow < deadline)
            {
                Process[] procs = Process.GetProcessesByName(ProcessName);
                foreach (Process p in procs)
                {
                    try
                    {
                        // 优先匹配启动后的新进程（不在旧 PID 集合中）
                        if (!existingPids.Contains(p.Id) && p.StartTime.ToUniversalTime() >= after)
                        {
                            // 释放其余进程，避免资源泄漏
                            foreach (var other in procs)
                            {
                                if (other.Id != p.Id) other.Dispose();
                            }
                            return p;
                        }
                    }
                    catch (Exception ex)
                    {
                        Console.Error.WriteLine($"[BRIDGE] Warning: 访问进程信息失败: {ex.Message}");
                    }
                }
                // fallback：如果有新进程但无法获取 StartTime，返回第一个非旧进程
                foreach (Process p in procs)
                {
                    if (!existingPids.Contains(p.Id))
                    {
                        try
                        {
                            // 释放其余进程，避免资源泄漏
                            foreach (var other in procs)
                            {
                                if (other.Id != p.Id) other.Dispose();
                            }
                            return p;
                        }
                        catch (Exception ex)
                        {
                            Console.Error.WriteLine($"[BRIDGE] Warning: 返回进程失败: {ex.Message}");
                        }
                    }
                }
                foreach (var p in procs) p.Dispose();
                Thread.Sleep(1000);
            }
            return null;
        }

        private static int GetEnvInt(string name, int defaultValue)
        {
            string value = Environment.GetEnvironmentVariable(name);
            if (!string.IsNullOrEmpty(value) && int.TryParse(value, out int parsed) && parsed > 0)
            {
                return parsed;
            }
            return defaultValue;
        }

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
                            Console.WriteLine($"[BRIDGE]   主窗口已检测到: \"{title}\"");
                            mainWindowFound = true;
                        }
                    }
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine($"[BRIDGE] Warning: 主窗口检测异常: {ex.Message}");
                }
                if (!mainWindowFound) Thread.Sleep(1000);
            }

            if (!mainWindowFound)
            {
                Console.WriteLine("[BRIDGE]   Phase 1 超时, 使用 WaitForInputIdle 兜底...");
                try
                {
                    if (p.WaitForInputIdle(15000))
                    {
                        Console.WriteLine("[BRIDGE]   WaitForInputIdle 兜底 OK");
                        Thread.Sleep(10000);
                        return;
                    }
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine($"[BRIDGE] Warning: WaitForInputIdle 异常: {ex.Message}");
                }
                Console.WriteLine("[BRIDGE]   兜底失败, 使用固定延时 (20s)");
                Thread.Sleep(20000);
                return;
            }

            Console.WriteLine("[BRIDGE] Phase 2: 等待主窗口线程空闲...");
            try
            {
                p.Refresh();
                if (p.WaitForInputIdle(15000))
                {
                    Console.WriteLine("[BRIDGE]   主窗口线程已空闲");
                }
                else
                {
                    Console.WriteLine("[BRIDGE]   WaitForInputIdle 超时, 继续...");
                }
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[BRIDGE] Warning: Phase 2 WaitForInputIdle 异常: {ex.Message}");
            }

            // Phase 3 等待时间可通过环境变量配置（默认 15 秒）
            // AUTOFLUID_SC_GUI_STABLE_DELAY 优先，向后兼容 AUTOFLUID_SC_GUI_WAIT
            int guiWaitSeconds = GetEnvInt("AUTOFLUID_SC_GUI_STABLE_DELAY", 0);
            if (guiWaitSeconds <= 0)
            {
                guiWaitSeconds = GetEnvInt("AUTOFLUID_SC_GUI_WAIT", 15);
            }
            Console.WriteLine($"[BRIDGE] Phase 3: 等待加载稳定 (延时 {guiWaitSeconds}s)...");
            Thread.Sleep(guiWaitSeconds * 1000);
            Console.WriteLine("[BRIDGE] SpaceClaim GUI 加载完成");
        }
    }

    internal class BridgeOptions
    {
        public string ScriptPath { get; set; }
        public string ConfigName { get; set; }
        public string StepDir { get; set; }
        public string ScdocDir { get; set; }
        public int TimeoutSeconds { get; set; } = 300;
        public string ScExePath { get; set; }
        public bool Persistent { get; set; }
        public string CmdDir { get; set; }
        public int SlotId { get; set; }
    }
}
