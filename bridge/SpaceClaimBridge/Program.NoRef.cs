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
    /// SpaceClaim Bridge — C# 5 兼容版本，纯进程检测模式
    ///
    /// SpaceClaim 不向外部暴露 out-of-process COM 自动化接口（与 AutoCAD/SolidWorks 不同），
    /// 因此采用命令行 /RunScript + 进程检测的纯进程模式。
    ///
    /// 用法:
    ///   SpaceClaimBridge.exe --script &lt;transit.py&gt; --config &lt;N&gt; --stepdir &lt;dir&gt; --scdocdir &lt;dir&gt; [--timeout &lt;s&gt;]
    ///
    /// 返回值: 0=成功 1=脚本失败 2=启动失败 3=输出验证失败 4=参数错误 5=超时
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
                BridgeOptions opts = ParseArguments(args);
                if (opts == null) return (int)ExitCode.InvalidArgs;
                return Execute(opts);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("[BRIDGE_FATAL] " + ex.ToString());
                return (int)ExitCode.ScriptFailed;
            }
        }

        class BridgeOptions
        {
            public string Script { get; set; }
            public string Config { get; set; }
            public string StepDir { get; set; }
            public string ScdocDir { get; set; }
            public int Timeout { get; set; } = 300;
            public string ScExe { get; set; }
            public bool Persistent { get; set; }
            public string CmdDir { get; set; }
            public int SlotId { get; set; }
        }

        static BridgeOptions ParseArguments(string[] args)
        {
            BridgeOptions o = new BridgeOptions();
            for (int i = 0; i < args.Length; i++)
            {
                switch (args[i].ToLowerInvariant())
                {
                    case "--script":
                        if (++i < args.Length) o.Script = args[i];
                        break;
                    case "--config":
                        if (++i < args.Length) o.Config = args[i];
                        break;
                    case "--stepdir":
                        if (++i < args.Length) o.StepDir = args[i];
                        break;
                    case "--scdocdir":
                        if (++i < args.Length) o.ScdocDir = args[i];
                        break;
                    case "--timeout":
                        if (++i < args.Length)
                        {
                            int t;
                            if (int.TryParse(args[i], out t)) o.Timeout = t;
                        }
                        break;
                    case "--sc-exe":
                        if (++i < args.Length) o.ScExe = args[i];
                        break;
                    case "--persistent":
                        o.Persistent = true;
                        break;
                    case "--cmddir":
                        if (++i < args.Length) o.CmdDir = args[i];
                        break;
                    case "--slotid":
                        if (++i < args.Length)
                        {
                            int sid;
                            if (int.TryParse(args[i], out sid)) o.SlotId = sid;
                        }
                        break;
                    default:
                        Console.Error.WriteLine("[BRIDGE_ERROR] 未知参数: " + args[i]);
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
                        return null;
                }
            }
            if (string.IsNullOrEmpty(o.Script) || string.IsNullOrEmpty(o.Config) ||
                string.IsNullOrEmpty(o.StepDir) || string.IsNullOrEmpty(o.ScdocDir))
            {
                if (o.Persistent)
                {
                    if (string.IsNullOrEmpty(o.Script) || string.IsNullOrEmpty(o.CmdDir))
                    {
                        Console.Error.WriteLine("[BRIDGE_ERROR] 常驻模式缺少必要参数 (--script, --cmddir)");
                        return null;
                    }
                }
                else
                {
                    Console.Error.WriteLine("[BRIDGE_ERROR] 缺少必要参数");
                    return null;
                }
            }
            return o;
        }

        static int Execute(BridgeOptions o)
        {
            if (o.Persistent)
            {
                return ExecutePersistent(o);
            }

            Console.WriteLine("[BRIDGE] SpaceClaim Bridge 启动");
            Console.WriteLine(string.Format("[BRIDGE]   脚本: {0}", o.Script));
            Console.WriteLine(string.Format("[BRIDGE]   构型: {0}", o.Config));
            Console.WriteLine(string.Format("[BRIDGE]   STEP目录: {0}", o.StepDir));
            Console.WriteLine(string.Format("[BRIDGE]   SCDOC目录: {0}", o.ScdocDir));
            Console.WriteLine(string.Format("[BRIDGE]   超时: {0}s", o.Timeout));

            string stepFile = Path.Combine(o.StepDir, "model_gen4.SLDPRT_" + o.Config + ".step");
            if (!File.Exists(stepFile))
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] STEP 文件不存在: " + stepFile);
                return (int)ExitCode.OutputValidationFailed;
            }
            Console.WriteLine(string.Format("[BRIDGE] STEP: {0} ({1} B)", stepFile, new FileInfo(stepFile).Length));

            Directory.CreateDirectory(o.ScdocDir);

            string scExe = FindSpaceClaimExe(o.ScExe);
            if (scExe == null)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim.exe 未找到");
                return (int)ExitCode.LaunchFailed;
            }

            DateTime launchBaseline = DateTime.UtcNow;

            string runScriptArg = string.Format("/RunScript=\"{0}\"", o.Script);

            Console.WriteLine("[BRIDGE] 正在启动 SpaceClaim (环境变量传参模式)...");
            Console.WriteLine(string.Format("[BRIDGE]   Exe: {0}", scExe));
            Console.WriteLine(string.Format("[BRIDGE]   Args: {0} /Splash=False /Welcome=False /ExitAfterScript=True",
                runScriptArg));
            Console.WriteLine(string.Format("[BRIDGE]   Env: AUTOFLUID_SC_CONFIG={0}", o.Config));
            Console.WriteLine(string.Format("[BRIDGE]   Env: AUTOFLUID_SC_STEP_DIR={0}", o.StepDir));
            Console.WriteLine(string.Format("[BRIDGE]   Env: AUTOFLUID_SC_SCDOC_DIR={0}", o.ScdocDir));

            try
            {
                ProcessStartInfo psi = new ProcessStartInfo
                {
                    FileName = scExe,
                    Arguments = runScriptArg + " /Splash=False /Welcome=False /ExitAfterScript=True",
                    UseShellExecute = false,
                };
                psi.EnvironmentVariables["AUTOFLUID_SC_CONFIG"] = o.Config;
                psi.EnvironmentVariables["AUTOFLUID_SC_STEP_DIR"] = o.StepDir;
                psi.EnvironmentVariables["AUTOFLUID_SC_SCDOC_DIR"] = o.ScdocDir;
                using (Process started = Process.Start(psi))
                {
                    // 仅用于触发启动，句柄由 WaitForProcessAppear 重新获取
                }
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] 启动 SpaceClaim 失败: " + ex.Message);
                return (int)ExitCode.LaunchFailed;
            }

            Console.WriteLine("[BRIDGE] 等待 SpaceClaim 进程出现...");
            Process workingProcess = WaitForProcessAppear(launchBaseline, 120);
            if (workingProcess == null)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim 进程在 120s 内未出现");
                return (int)ExitCode.LaunchFailed;
            }

            Console.WriteLine(string.Format("[BRIDGE] SpaceClaim 进程已出现 (PID={0}), 等待 GUI 就绪...", workingProcess.Id));
            WaitForGuiReady(workingProcess, 30);

            Console.WriteLine("[BRIDGE] SpaceClaim 正在运行, 监控脚本执行完成...");

            int totalTimeout = o.Timeout;
            int pollIntervalMs = 2000;
            DateTime deadline = DateTime.UtcNow.AddSeconds(totalTimeout);
            string scdocFile = Path.Combine(o.ScdocDir, "model_gen4_" + o.Config + ".scdoc");

            while (DateTime.UtcNow < deadline)
            {
                if (File.Exists(scdocFile))
                {
                    FileInfo fi = new FileInfo(scdocFile);
                    Console.WriteLine(string.Format("[BRIDGE] ✓ SCDOC 已生成: {0} ({1} B)", scdocFile, fi.Length));
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
                    Console.Error.WriteLine("[BRIDGE] Warning: 进程状态检查失败: " + ex.Message);
                    processAlive = false;
                }

                if (!processAlive)
                {
                    Console.WriteLine("[BRIDGE] SpaceClaim 进程已退出, 最终检查...");
                    Thread.Sleep(2000);
                    if (File.Exists(scdocFile))
                    {
                        FileInfo fi = new FileInfo(scdocFile);
                        Console.WriteLine(string.Format("[BRIDGE] ✓ SCDOC 已生成: {0} ({1} B)", scdocFile, fi.Length));
                        return (int)ExitCode.Success;
                    }
                    Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim 已退出但未生成 SCDOC 文件");
                    return (int)ExitCode.OutputValidationFailed;
                }

                Thread.Sleep(pollIntervalMs);
            }

            Console.Error.WriteLine(string.Format("[BRIDGE_ERROR] 超时 ({0}s)", totalTimeout));
            return (int)ExitCode.Timeout;
        }

        static int ExecutePersistent(BridgeOptions o)
        {
            Console.WriteLine("[BRIDGE] 常驻模式启动");
            Console.WriteLine("[BRIDGE]   脚本: " + o.Script);
            Console.WriteLine("[BRIDGE]   槽位: " + o.SlotId);
            Console.WriteLine("[BRIDGE]   IPC目录: " + o.CmdDir);

            if (string.IsNullOrEmpty(o.CmdDir))
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] 常驻模式需要 --cmddir 参数");
                return (int)ExitCode.InvalidArgs;
            }

            Directory.CreateDirectory(o.CmdDir);

            string scExe = FindSpaceClaimExe(o.ScExe);
            if (scExe == null)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim.exe 未找到");
                return (int)ExitCode.LaunchFailed;
            }

            DateTime launchBaseline = DateTime.UtcNow;
            string runScriptArg = string.Format("/RunScript=\"{0}\"", o.Script);

            Console.WriteLine("[BRIDGE] 正在启动 SpaceClaim (常驻模式)...");
            Console.WriteLine("[BRIDGE]   Exe: " + scExe);
            Console.WriteLine("[BRIDGE]   Args: " + runScriptArg + " /Splash=False /Welcome=False");
            Console.WriteLine("[BRIDGE]   Env: AUTOFLUID_SC_PERSISTENT=1");
            Console.WriteLine("[BRIDGE]   Env: AUTOFLUID_SC_CMD_DIR=" + o.CmdDir);
            Console.WriteLine("[BRIDGE]   Env: AUTOFLUID_SC_SLOT_ID=" + o.SlotId);

            try
            {
                ProcessStartInfo psi = new ProcessStartInfo
                {
                    FileName = scExe,
                    // 常驻模式不使用 /ExitAfterScript=True
                    Arguments = runScriptArg + " /Splash=False /Welcome=False",
                    UseShellExecute = false,
                };
                psi.EnvironmentVariables["AUTOFLUID_SC_NOEXIT"] = "1";
                psi.EnvironmentVariables["AUTOFLUID_SC_PERSISTENT"] = "1";
                psi.EnvironmentVariables["AUTOFLUID_SC_CMD_DIR"] = o.CmdDir;
                psi.EnvironmentVariables["AUTOFLUID_SC_SLOT_ID"] = o.SlotId.ToString();
                using (Process started = Process.Start(psi))
                {
                    // 仅用于触发启动，句柄由 WaitForProcessAppear 重新获取
                }
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] 启动 SpaceClaim 失败: " + ex.Message);
                return (int)ExitCode.LaunchFailed;
            }

            Console.WriteLine("[BRIDGE] 等待 SpaceClaim 进程出现...");
            Process workingProcess = WaitForProcessAppear(launchBaseline, 120);
            if (workingProcess == null)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim 进程在 120s 内未出现");
                return (int)ExitCode.LaunchFailed;
            }

            Console.WriteLine(string.Format("[BRIDGE] SpaceClaim 进程已出现 (PID={0}), 等待 GUI 就绪...", workingProcess.Id));
            WaitForGuiReady(workingProcess, 30);

            // 等待脚本就绪标志
            string readyFile = Path.Combine(o.CmdDir, string.Format("sc_ready_{0}.json", o.SlotId));
            Console.WriteLine("[BRIDGE] 等待脚本就绪标志: " + readyFile);
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
                        break;
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
                    Console.Error.WriteLine("[BRIDGE] Warning: 进程状态检查异常: " + ex.Message);
                    return (int)ExitCode.LaunchFailed;
                }
                Thread.Sleep(2000);
            }

            if (!File.Exists(readyFile))
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] 脚本就绪超时 (120s)");
                return (int)ExitCode.Timeout;
            }

            Console.WriteLine("[BRIDGE] 常驻模式就绪，开始命令循环...");

            string cmdFile = Path.Combine(o.CmdDir, string.Format("sc_cmd_{0}.json", o.SlotId));

            // 命令循环：监听 quit 文件命令或进程退出
            int exitCode = (int)ExitCode.Success;
            // ★ 进程检测连续失败计数器：防止因瞬态异常（如进程句柄暂不可用）
            //    误判 SpaceClaim 退出。累计 5 次连续失败才确认退出。
            int consecutiveProcessCheckFailures = 0;
            const int maxConsecutiveFailures = 5;

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

                    // ★ 检测 SpaceClaim 进程是否已退出（容错增强版）
                    //    单次 Refresh/HasExited 可能因瞬态异常（Win32Exception、
                    //    InvalidOperationException）失败，不能直接判定退出。
                    //    连续 N 次失败后才确认进程确实已退出。
                    try
                    {
                        workingProcess.Refresh();
                        if (workingProcess.HasExited)
                        {
                            Console.WriteLine("[BRIDGE] SpaceClaim 进程已退出，Bridge 退出");
                            break;
                        }
                        // 检测成功 → 重置失败计数器
                        consecutiveProcessCheckFailures = 0;
                    }
                    catch (InvalidOperationException)
                    {
                        // 进程对象已释放 → 确认退出
                        Console.WriteLine("[BRIDGE] SpaceClaim 进程对象已释放，Bridge 退出");
                        break;
                    }
                    catch (Exception ex)
                    {
                        consecutiveProcessCheckFailures++;
                        Console.Error.WriteLine(
                            string.Format("[BRIDGE] Warning: 进程状态检查异常 ({0}/{1}): {2}",
                                consecutiveProcessCheckFailures, maxConsecutiveFailures, ex.Message));
                        if (consecutiveProcessCheckFailures >= maxConsecutiveFailures)
                        {
                            Console.Error.WriteLine(
                                "[BRIDGE] 进程状态检查连续失败，判定 SpaceClaim 已退出");
                            break;
                        }
                        // 短暂等待后重试
                        Thread.Sleep(2000);
                        continue;
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

        static string FindSpaceClaimExe(string userPath = null)
        {
            // 1. 优先使用命令行指定的路径
            if (!string.IsNullOrEmpty(userPath) && File.Exists(userPath))
            {
                Console.WriteLine("[BRIDGE] 找到 SpaceClaim.exe (命令行): " + userPath);
                return userPath;
            }
            // 2. 检查环境变量 AUTOFLUID_SC_EXE
            string envPath = Environment.GetEnvironmentVariable("AUTOFLUID_SC_EXE");
            if (!string.IsNullOrEmpty(envPath) && File.Exists(envPath))
            {
                Console.WriteLine("[BRIDGE] 找到 SpaceClaim.exe (环境变量): " + envPath);
                return envPath;
            }
            // 3. 自动检测已知安装路径
            foreach (string p in SpaceClaimExePaths)
            {
                if (File.Exists(p))
                {
                    Console.WriteLine("[BRIDGE] 找到 SpaceClaim.exe: " + p);
                    return p;
                }
            }
            return null;
        }

        static Process WaitForProcessAppear(DateTime after, int timeoutSec)
        {
            // 记录启动前已有的进程 PID，用于过滤旧进程
            var existingPids = new HashSet<int>();
            foreach (Process ep in Process.GetProcessesByName(ProcessName))
            {
                try { existingPids.Add(ep.Id); } catch (Exception ex) { Console.Error.WriteLine("[BRIDGE] Warning: 记录旧PID失败: " + ex.Message); }
                ep.Dispose();
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
                        Console.Error.WriteLine("[BRIDGE] Warning: 访问进程信息失败: " + ex.Message);
                    }
                }
                // fallback：返回第一个非旧进程
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
                            Console.Error.WriteLine("[BRIDGE] Warning: 返回进程失败: " + ex.Message);
                        }
                    }
                }
                foreach (var p in procs) p.Dispose();
                Thread.Sleep(1000);
            }
            return null;
        }

        static void WaitForGuiReady(Process p, int timeoutSec)
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
                            Console.WriteLine(string.Format("[BRIDGE]   主窗口已检测到: \"{0}\"", title));
                            mainWindowFound = true;
                        }
                    }
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine("[BRIDGE] Warning: 主窗口检测异常: " + ex.Message);
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
                    Console.Error.WriteLine("[BRIDGE] Warning: WaitForInputIdle 异常: " + ex.Message);
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
                Console.Error.WriteLine("[BRIDGE] Warning: Phase 2 WaitForInputIdle 异常: " + ex.Message);
            }

            // Phase 3 等待时间可通过环境变量 AUTOFLUID_SC_GUI_STABLE_DELAY 配置（默认 15 秒）
            int guiWaitSeconds = 15;
            string envWait = Environment.GetEnvironmentVariable("AUTOFLUID_SC_GUI_STABLE_DELAY");
            if (!string.IsNullOrEmpty(envWait))
            {
                int parsed;
                if (int.TryParse(envWait, out parsed) && parsed > 0)
                {
                    guiWaitSeconds = parsed;
                }
            }
            Console.WriteLine(string.Format("[BRIDGE] Phase 3: 等待加载稳定 (延时 {0}s)...", guiWaitSeconds));
            Thread.Sleep(guiWaitSeconds * 1000);
            Console.WriteLine("[BRIDGE] SpaceClaim GUI 加载完成");
        }

    }
}
