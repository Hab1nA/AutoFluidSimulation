using System;
using System.Diagnostics;
using System.IO;
using System.Threading;

namespace AutoFluidSimulation.Bridge
{
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
                if (opts == null) return 4;
                return Execute(opts);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("[BRIDGE_FATAL] " + ex.ToString());
                return 1;
            }
        }

        class Options
        {
            public string Script, Config, StepDir, ScdocDir;
            public int Timeout = 300;
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
                    default:
                        Console.Error.WriteLine("未知参数: " + args[i]);
                        Console.Error.WriteLine("用法: SpaceClaimBridge.exe --script <path> --config <n> --stepdir <dir> --scdocdir <dir> [--timeout <s>]");
                        return null;
                }
            }
            if (string.IsNullOrEmpty(o.Script) || string.IsNullOrEmpty(o.Config) ||
                string.IsNullOrEmpty(o.StepDir) || string.IsNullOrEmpty(o.ScdocDir))
            {
                Console.Error.WriteLine("缺少必要参数");
                return null;
            }
            return o;
        }

        static int Execute(Options o)
        {
            Console.WriteLine(string.Format("[BRIDGE] script={0} config={1}", o.Script, o.Config));
            Console.WriteLine(string.Format("[BRIDGE] stepdir={0} scdocdir={1}", o.StepDir, o.ScdocDir));

            string stepFile = Path.Combine(o.StepDir, "model_gen4.SLDPRT_" + o.Config + ".step");
            if (!File.Exists(stepFile))
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] STEP file not found: " + stepFile);
                return 3;
            }
            Console.WriteLine(string.Format("[BRIDGE] STEP: {0} ({1} B)", stepFile, new FileInfo(stepFile).Length));

            Directory.CreateDirectory(o.ScdocDir);

            string scExe = FindSpaceClaimExe();
            if (scExe == null)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim.exe not found");
                return 2;
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
                Process.Start(psi);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] Failed to launch SpaceClaim: " + ex.Message);
                return 2;
            }

            Console.WriteLine("[BRIDGE] Waiting for SpaceClaim process to appear...");
            Process workingProcess = WaitForProcessAppear(launchBaseline, 120);
            if (workingProcess == null)
            {
                Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim process did not appear within 120s");
                return 2;
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
                    return 0;
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
                        return 0;
                    }
                    Console.Error.WriteLine("[BRIDGE_ERROR] SpaceClaim exited but no SCDOC file generated");
                    return 3;
                }

                Thread.Sleep(pollIntervalMs);
            }

            Console.Error.WriteLine(string.Format("[BRIDGE_ERROR] Timeout ({0}s)", totalTimeout));
            return 5;
        }

        static string FindSpaceClaimExe()
        {
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
            DateTime deadline = DateTime.UtcNow.AddSeconds(timeoutSec);
            while (DateTime.UtcNow < deadline)
            {
                Process[] procs = Process.GetProcessesByName(ProcessName);
                foreach (Process p in procs)
                {
                    try
                    {
                        if (p.StartTime.ToUniversalTime() >= after)
                        {
                            return p;
                        }
                    }
                    catch { }
                }
                if (procs.Length > 0)
                {
                    try { return procs[0]; }
                    catch { }
                }
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
                catch { }
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
                catch { }
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
            catch
            {
                Console.WriteLine("[BRIDGE]   WaitForInputIdle exception, continuing...");
            }

            Console.WriteLine("[BRIDGE] Phase 3: Waiting for loading stabilization (fixed 15s)...");
            Thread.Sleep(15000);
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
