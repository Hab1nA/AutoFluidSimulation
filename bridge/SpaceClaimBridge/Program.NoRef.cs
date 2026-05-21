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
                Options opts = ParseArgs(args);
                if (opts == null) return (int)ExitCode.InvalidArgs;
                return Execute(opts);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("[BRIDGE_FATAL] " + ex.ToString());
                return (int)ExitCode.ScriptFailed;
            }
        }

        class Options
        {
            public string Script { get; set; }
            public string Config { get; set; }
            public string StepDir { get; set; }
            public string ScdocDir { get; set; }
            public int Timeout { get; set; } = 300;
            public bool Persistent { get; set; }
            public string CmdDir { get; set; }
            public int SlotId { get; set; }
        }

        static Options ParseArgs(string[] args)
        {
            Options o = new Options();
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
                        Console.Error.WriteLine("未知参数: " + args[i]);
                        Console.Error.WriteLine("用法: SpaceClaimBridge.exe --script <path> --config <n> --stepdir <dir> --scdocdir <dir> [--timeout <s>]");
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
                        Console.Error.WriteLine("Persistent mode requires --script and --cmddir");
                        return null;
                    }
                }
                else
                {
                    Console.Error.WriteLine("缺少必要参数");
                    return null;
                }
            }
            return o;
        }

        static int Execute(Options o)
        {
            if (o.Persistent)
            {
                return ExecutePersistent(o);
            }

            Console.WriteLine(string.Format("[BRIDGE] script={0} config={1}", o.Script, o.Config));
            Console.WriteLine(string.Format("[BRIDGE] stepdir={0} scdocdir={1}", o.StepDir, o.ScdocDir));

            string stepFile = Path.Combine(o.StepDir, "model_gen4.SLDPRT_" + o.Config + ".step");
            if (!File.Exists(stepFile))
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] STEP file not found: " + stepFile);
                return (int)ExitCode.OutputValidationFailed;
            }
            Console.WriteLine(string.Format("[BRIDGE] STEP: {0} ({1} B)", stepFile, new FileInfo(stepFile).Length));

            Directory.CreateDirectory(o.ScdocDir);

            string scExe = FindSpaceClaimExe();
            if (scExe == null)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim.exe not found");
                return (int)ExitCode.LaunchFailed;
            }

            DateTime launchBaseline = DateTime.UtcNow;

            string runScriptArg = string.Format("/RunScript=\"{0}\"", o.Script);

            Console.WriteLine("[BRIDGE] Launching SpaceClaim with /RunScript (env vars mode)...");
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
                Console.Error.WriteLine("[BRIDGE_ERROR] Failed to launch SpaceClaim: " + ex.Message);
                return (int)ExitCode.LaunchFailed;
            }

            Console.WriteLine("[BRIDGE] Waiting for SpaceClaim process to appear...");
            Process workingProcess = WaitForProcessAppear(launchBaseline, 120);
            if (workingProcess == null)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim process did not appear within 120s");
                return (int)ExitCode.LaunchFailed;
            }

            Console.WriteLine(string.Format("[BRIDGE] SpaceClaim process found (PID={0}), waiting for GUI...", workingProcess.Id));
            WaitForGuiReady(workingProcess, 30);

            Console.WriteLine("[BRIDGE] SpaceClaim is running. Monitoring for completion...");

            int totalTimeout = o.Timeout;
            int pollIntervalMs = 2000;
            DateTime deadline = DateTime.UtcNow.AddSeconds(totalTimeout);
            string scdocFile = Path.Combine(o.ScdocDir, "model_gen4_" + o.Config + ".scdoc");

            while (DateTime.UtcNow < deadline)
            {
                if (File.Exists(scdocFile))
                {
                    FileInfo fi = new FileInfo(scdocFile);
                    Console.WriteLine(string.Format("[BRIDGE] OK SCDOC detected: {0} ({1} B)", scdocFile, fi.Length));
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
                catch
                {
                    processAlive = false;
                }

                if (!processAlive)
                {
                    Console.WriteLine("[BRIDGE] SpaceClaim process exited. Final output check...");
                    Thread.Sleep(2000);
                    if (File.Exists(scdocFile))
                    {
                        FileInfo fi = new FileInfo(scdocFile);
                        Console.WriteLine(string.Format("[BRIDGE] OK SCDOC: {0} ({1} B)", scdocFile, fi.Length));
                        return (int)ExitCode.Success;
                    }
                    Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim exited but no SCDOC file generated");
                    return (int)ExitCode.OutputValidationFailed;
                }

                Thread.Sleep(pollIntervalMs);
            }

            Console.Error.WriteLine(string.Format("[BRIDGE_ERROR] Timeout ({0}s)", totalTimeout));
            return (int)ExitCode.Timeout;
        }

        static int ExecutePersistent(Options o)
        {
            Console.WriteLine("[BRIDGE] Persistent mode starting");
            Console.WriteLine("[BRIDGE]   Script: " + o.Script);
            Console.WriteLine("[BRIDGE]   SlotId: " + o.SlotId);
            Console.WriteLine("[BRIDGE]   CmdDir: " + o.CmdDir);

            if (string.IsNullOrEmpty(o.CmdDir))
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] Persistent mode requires --cmddir");
                return (int)ExitCode.InvalidArgs;
            }

            Directory.CreateDirectory(o.CmdDir);

            string scExe = FindSpaceClaimExe();
            if (scExe == null)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim.exe not found");
                return (int)ExitCode.LaunchFailed;
            }

            DateTime launchBaseline = DateTime.UtcNow;
            string runScriptArg = string.Format("/RunScript=\"{0}\"", o.Script);

            Console.WriteLine("[BRIDGE] Launching SpaceClaim (persistent mode)...");
            Console.WriteLine("[BRIDGE]   Env: AUTOFLUID_SC_PERSISTENT=1");

            try
            {
                ProcessStartInfo psi = new ProcessStartInfo
                {
                    FileName = scExe,
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
                Console.Error.WriteLine("[BRIDGE_ERROR] Failed to launch SpaceClaim: " + ex.Message);
                return (int)ExitCode.LaunchFailed;
            }

            Console.WriteLine("[BRIDGE] Waiting for SpaceClaim process...");
            Process workingProcess = WaitForProcessAppear(launchBaseline, 120);
            if (workingProcess == null)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim process did not appear within 120s");
                return (int)ExitCode.LaunchFailed;
            }

            Console.WriteLine(string.Format("[BRIDGE] SpaceClaim found (PID={0}), waiting for GUI...", workingProcess.Id));
            WaitForGuiReady(workingProcess, 30);

            // Wait for script ready flag
            string readyFile = Path.Combine(o.CmdDir, string.Format("sc_ready_{0}.json", o.SlotId));
            Console.WriteLine("[BRIDGE] Waiting for script ready flag: " + readyFile);
            DateTime readyDeadline = DateTime.UtcNow.AddSeconds(120);
            while (DateTime.UtcNow < readyDeadline)
            {
                if (File.Exists(readyFile))
                {
                    Console.WriteLine("[BRIDGE] Script ready flag detected");
                    break;
                }
                try { workingProcess.Refresh(); if (workingProcess.HasExited) break; }
                catch (Exception ex) { Console.Error.WriteLine("[BRIDGE] Warning: 进程检查失败: " + ex.Message); break; }
                Thread.Sleep(2000);
            }

            if (!File.Exists(readyFile))
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] Script ready timeout (120s)");
                return (int)ExitCode.Timeout;
            }

            Console.WriteLine("[BRIDGE] Persistent mode ready, monitoring...");

            while (true)
            {
                try
                {
                    workingProcess.Refresh();
                    if (workingProcess.HasExited)
                    {
                        Console.WriteLine("[BRIDGE] SpaceClaim exited, Bridge exiting");
                        break;
                    }
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine("[BRIDGE] Warning: 进程检查失败: " + ex.Message);
                    break;
                }
                Thread.Sleep(2000);
            }

            Console.WriteLine("[BRIDGE] Persistent mode exited");
            return (int)ExitCode.Success;
        }

        static string FindSpaceClaimExe()
        {
            // 优先检查环境变量 AUTOFLUID_SC_EXE
            string envPath = Environment.GetEnvironmentVariable("AUTOFLUID_SC_EXE");
            if (!string.IsNullOrEmpty(envPath) && File.Exists(envPath))
            {
                Console.WriteLine("[BRIDGE] Found SpaceClaim.exe (env): " + envPath);
                return envPath;
            }
            foreach (string p in SpaceClaimExePaths)
            {
                if (File.Exists(p))
                {
                    Console.WriteLine("[BRIDGE] Found SpaceClaim.exe: " + p);
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
                        try { return p; }
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

            Console.WriteLine("[BRIDGE] Phase 1: Waiting for SpaceClaim main window...");
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
                            Console.WriteLine(string.Format("[BRIDGE]   Main window detected: \"{0}\"", title));
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
                Console.WriteLine("[BRIDGE]   Phase 1 timed out, trying WaitForInputIdle fallback...");
                try
                {
                    if (p.WaitForInputIdle(15000))
                    {
                        Console.WriteLine("[BRIDGE]   WaitForInputIdle fallback OK");
                        Thread.Sleep(10000);
                        return;
                    }
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine("[BRIDGE] Warning: WaitForInputIdle 异常: " + ex.Message);
                }
                Console.WriteLine("[BRIDGE]   Fallback failed, using fixed delay (20s)");
                Thread.Sleep(20000);
                return;
            }

            Console.WriteLine("[BRIDGE] Phase 2: Waiting for main window thread idle...");
            try
            {
                p.Refresh();
                if (p.WaitForInputIdle(15000))
                {
                    Console.WriteLine("[BRIDGE]   Main window thread is idle");
                }
                else
                {
                    Console.WriteLine("[BRIDGE]   WaitForInputIdle timed out, continuing...");
                }
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("[BRIDGE] Warning: Phase 2 WaitForInputIdle 异常: " + ex.Message);
            }

            // Phase 3 等待时间可通过环境变量 AUTOFLUID_SC_GUI_WAIT 配置（默认 15 秒）
            int guiWaitSeconds = 15;
            string envWait = Environment.GetEnvironmentVariable("AUTOFLUID_SC_GUI_WAIT");
            if (!string.IsNullOrEmpty(envWait))
            {
                int parsed;
                if (int.TryParse(envWait, out parsed) && parsed > 0)
                {
                    guiWaitSeconds = parsed;
                }
            }
            Console.WriteLine(string.Format("[BRIDGE] Phase 3: Waiting for loading stabilization ({0}s)...", guiWaitSeconds));
            Thread.Sleep(guiWaitSeconds * 1000);
            Console.WriteLine("[BRIDGE] SpaceClaim GUI loading complete");
        }

        static string EnsureQuoted(string path)
        {
            if (path.Contains(" "))
            {
                return "\"" + path + "\"";
            }
            return path;
        }
    }
}
