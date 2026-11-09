using System;
using System.Collections.Generic;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Automation;
using System.Windows.Forms;

namespace WorkObserver
{
    internal sealed class Raw
    {
        public string Kind;
        public int Key, X, Y, Pid;
        public long Time;
        public IntPtr Window;
        public bool Ctrl, Alt, Shift, Win;
    }
    internal static class Native
    {
        internal delegate IntPtr HookProc(int code, IntPtr w, IntPtr l);
        [StructLayout(LayoutKind.Sequential)] internal struct Point { public int X, Y; }
        [StructLayout(LayoutKind.Sequential)] internal struct Mouse { public Point Pt; public uint Data, Flags, Time; public UIntPtr Extra; }
        [StructLayout(LayoutKind.Sequential)] internal struct Keyboard { public uint Key, Scan, Flags, Time; public UIntPtr Extra; }
        [StructLayout(LayoutKind.Sequential)] internal struct Rect { public int Left, Top, Right, Bottom; }
        [DllImport("user32.dll", SetLastError=true)] internal static extern IntPtr SetWindowsHookEx(int id, HookProc proc, IntPtr mod, uint thread);
        [DllImport("user32.dll")] internal static extern bool UnhookWindowsHookEx(IntPtr hook);
        [DllImport("user32.dll")] internal static extern IntPtr CallNextHookEx(IntPtr hook, int code, IntPtr w, IntPtr l);
        [DllImport("kernel32.dll", CharSet=CharSet.Auto)] internal static extern IntPtr GetModuleHandle(string name);
        [DllImport("user32.dll")] internal static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] internal static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
        [DllImport("user32.dll")] internal static extern short GetAsyncKeyState(int key);
        [DllImport("user32.dll")] internal static extern bool GetWindowRect(IntPtr window, out Rect rect);
        [DllImport("user32.dll")] internal static extern bool SetForegroundWindow(IntPtr window);
        [DllImport("user32.dll")] internal static extern bool RegisterHotKey(IntPtr window, int id, uint modifiers, uint key);
        [DllImport("user32.dll")] internal static extern bool UnregisterHotKey(IntPtr window, int id);
        [DllImport("user32.dll")] internal static extern bool SetProcessDPIAware();
        internal static bool Down(int key) { return (GetAsyncKeyState(key) & 0x8000) != 0; }
    }
    public static class Storage
    {
        public static readonly string Folder = System.IO.Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "WorkObserver", "Sessions");
        private static JavaScriptSerializer Serializer()
        { return new JavaScriptSerializer { MaxJsonLength = 128*1024*1024, RecursionLimit = 64 }; }
        public static string Save(Session session)
        {
            Analysis.Validate(session);
            Directory.CreateDirectory(Folder);
            string file = System.IO.Path.Combine(Folder, DateTime.Now.ToString("yyyyMMdd-HHmmss") + "-" + Guid.NewGuid().ToString("N").Substring(0,6) + ".wos");
            byte[] plain = Encoding.UTF8.GetBytes(Serializer().Serialize(session));
            try { File.WriteAllBytes(file, ProtectedData.Protect(plain, null, DataProtectionScope.CurrentUser)); }
            finally { Array.Clear(plain, 0, plain.Length); }
            return file;
        }
        public static Session Load(string file)
        {
            if (new FileInfo(file).Length > 130*1024*1024) throw new InvalidDataException("الملف كبير جدًا.");
            byte[] plain = ProtectedData.Unprotect(File.ReadAllBytes(file), null, DataProtectionScope.CurrentUser);
            try { var session = Serializer().Deserialize<Session>(Encoding.UTF8.GetString(plain)); Analysis.Validate(session); return session; }
            finally { Array.Clear(plain, 0, plain.Length); }
        }
    }
    internal sealed class Recorder : IDisposable
    {
        private readonly Dictionary<int, Target> selected;
        private readonly BlockingCollection<Raw> queue = new BlockingCollection<Raw>(1000);
        private readonly Stopwatch clock = new Stopwatch();
        private readonly object gate = new object();
        private readonly bool saveText, pictures;
        private readonly Session session = new Session();
        private Native.HookProc mouseProc, keyboardProc;
        private IntPtr mouseHook, keyboardHook;
        private Thread worker;
        private volatile bool active, paused;
        private int dropped, inspectedErrors, imageCount;
        private long lastImage = -2000, imageBytes;
        private bool outside;
        private AutomationElement pendingElement;
        private int[] pendingRuntime;
        private Step pendingText;
        private IntPtr pendingWindow;
        private long pendingAt;
        public Recorder(IEnumerable<Target> targets, bool text, bool shots)
        { selected = targets.ToDictionary(t => t.Pid); saveText = text; pictures = shots; }
        public bool Paused { get { return paused; } }
        public int Count { get { lock(gate) return session.Steps.Count; } }
        public long Elapsed { get { return clock.ElapsedMilliseconds; } }
        public void Start()
        {
            mouseProc = OnMouse; keyboardProc = OnKeyboard;
            mouseHook = Native.SetWindowsHookEx(14, mouseProc, Native.GetModuleHandle(null), 0);
            keyboardHook = Native.SetWindowsHookEx(13, keyboardProc, Native.GetModuleHandle(null), 0);
            if (mouseHook == IntPtr.Zero || keyboardHook == IntPtr.Zero)
            { Dispose(); throw new InvalidOperationException("تعذر بدء التسجيل؛ قد تمنعه سياسة الجهاز."); }
            active = true; clock.Start();
            worker = new Thread(Consume); worker.IsBackground = true; worker.SetApartmentState(ApartmentState.MTA); worker.Start();
        }
        public void TogglePause()
        {
            paused = !paused;
            Enqueue(new Raw {Kind="gap", Time=clock.ElapsedMilliseconds});
        }
        private void Enqueue(Raw raw)
        {
            try { if (!queue.TryAdd(raw)) Interlocked.Increment(ref dropped); }
            catch (InvalidOperationException) { }
        }
        private Raw Context(string kind)
        {
            if (!active || paused) return null;
            IntPtr window = Native.GetForegroundWindow(); uint pid;
            Native.GetWindowThreadProcessId(window, out pid);
            if (!selected.ContainsKey((int)pid))
            {
                if (!outside) { outside=true; Enqueue(new Raw {Kind="gap",Time=clock.ElapsedMilliseconds}); }
                return null;
            }
            outside = false;
            return new Raw {Kind=kind,Window=window,Pid=(int)pid,Time=clock.ElapsedMilliseconds,
                Ctrl=Native.Down(0x11), Alt=Native.Down(0x12), Shift=Native.Down(0x10),
                Win=Native.Down(0x5B)||Native.Down(0x5C)};
        }
        private IntPtr OnMouse(int code, IntPtr w, IntPtr l)
        {
            if (code >= 0)
            {
                int msg=w.ToInt32();
                if (msg==0x201 || msg==0x202 || msg==0x204 || msg==0x20A || msg==0x20E)
                {
                    Native.Mouse m=(Native.Mouse)Marshal.PtrToStructure(l,typeof(Native.Mouse));
                    if ((m.Flags & 1)==0)
                    {
                        Raw raw=Context(msg==0x201 ? "leftDown" : msg==0x202 ? "leftUp" : msg==0x204 ? "right" : "scroll");
                        if (raw != null) { raw.X=m.Pt.X; raw.Y=m.Pt.Y; Enqueue(raw); }
                    }
                }
            }
            return Native.CallNextHookEx(mouseHook,code,w,l);
        }
        private IntPtr OnKeyboard(int code, IntPtr w, IntPtr l)
        {
            if (code >= 0 && (w.ToInt32()==0x100 || w.ToInt32()==0x104))
            {
                Native.Keyboard key=(Native.Keyboard)Marshal.PtrToStructure(l,typeof(Native.Keyboard));
                if ((key.Flags & 0x10)==0 && !IsModifier((int)key.Key))
                {
                    Raw raw=Context("key");
                    if (raw != null)
                    {
                        // Printable keys are deliberately not stored as raw key codes.
                        raw.Key=(raw.Ctrl||raw.Alt||raw.Win||IsNavigation((int)key.Key)) ? (int)key.Key : 0;
                        if (raw.Key==0) raw.Kind="typing";
                        Enqueue(raw);
                    }
                }
            }
            return Native.CallNextHookEx(keyboardHook,code,w,l);
        }
        private static bool IsModifier(int k)
        { return new [] {16,17,18,91,92,160,161,162,163,164,165}.Contains(k); }
        private static bool IsNavigation(int k)
        { return new [] {8,9,13,27,33,34,35,36,37,38,39,40,45,46}.Contains(k) || (k>=112 && k<=135); }
        private void Add(Step step)
        {
            lock(gate)
            {
                if (session.Steps.Count >= 10000) {
                    active=false; var last=session.Steps[session.Steps.Count-1];
                    last.Replayable=false; last.Description="انتهى التسجيل عند حد الجلسة؛ راجع النهاية يدويًا."; return;
                }
                step.Number=session.Steps.Count+1; session.Steps.Add(step);
            }
        }
        private void Manual(string message,long time)
        { Add(new Step {Description=message,Milliseconds=time}); }
        private Step downStep;
        private Raw downRaw;
        private void Consume()
        {
            while (!queue.IsCompleted)
            {
                Raw raw;
                if (!queue.TryTake(out raw,150)) { FlushText(false); continue; }
                try { ProcessRaw(raw); }
                catch { Interlocked.Increment(ref inspectedErrors); FlushText(true); Manual("تعذر فحص عنصر الشاشة؛ تحتاج مراجعة.",raw.Time); }
            }
            FlushText(true);
            if (downStep != null) { downStep.Replayable=false; downStep.Description="ضغطة غير مكتملة؛ تحتاج مراجعة."; }
        }
        private bool SameForeground(Raw raw)
        { return Native.GetForegroundWindow()==raw.Window; }
        private static bool SameRuntime(int[] a,int[] b)
        { return a!=null && b!=null && a.SequenceEqual(b); }
        private void ProcessRaw(Raw raw)
        {
            if (raw.Kind=="gap")
            {
                FlushText(true);
                if(downStep!=null) {downStep.Replayable=false;downStep.Description="ضغطة لم يكتمل تتبعها؛ تحتاج مراجعة.";}
                downStep=null; downRaw=null;
                Manual("توقف التسجيل أو انتقال خارج البرامج المختارة؛ أكمل هذه النقطة يدويًا.",raw.Time); return;
            }
            if (raw.Kind=="leftUp")
            {
                if (downStep!=null && downRaw!=null)
                {
                    if (Math.Abs(raw.X-downRaw.X)>6 || Math.Abs(raw.Y-downRaw.Y)>6 || raw.Window!=downRaw.Window || raw.Time-downRaw.Time>1000)
                    { downStep.Replayable=false; downStep.Description="سحب أو ضغطة مطولة؛ تحتاج تنفيذ يدوي."; }
                    downStep=null; downRaw=null;
                }
                return;
            }
            if (!SameForeground(raw) || clock.ElapsedMilliseconds-raw.Time>1500)
            { FlushText(true); Manual("تغيرت الشاشة قبل فحص الحدث؛ تحتاج مراجعة.",raw.Time); return; }
            AutomationElement element = raw.Kind=="typing" || raw.Kind=="key" ? AutomationElement.FocusedElement :
                AutomationElement.FromPoint(new System.Windows.Point(raw.X,raw.Y));
            if (element==null || element.Current.ProcessId!=raw.Pid)
            { FlushText(true); Manual("عنصر غير متاح أو خارج البرنامج المحدد.",raw.Time); return; }
            if (element.Current.IsPassword)
            { FlushText(true); Manual("إدخال محمي مستبعد من التسجيل؛ أدخله يدويًا عند الإعادة.",raw.Time); return; }
            if (raw.Kind=="typing")
            {
                int[] runtime=element.GetRuntimeId();
                if (pendingText!=null && !SameRuntime(runtime,pendingRuntime)) FlushText(true);
                if (pendingText==null)
                {
                    pendingElement=element; pendingRuntime=runtime; pendingWindow=raw.Window;
                    pendingText=new Step {Kind="typing",Description="كتابة داخل حقل؛ تحتاج مراجعة.",Target=Locate(element,raw),Milliseconds=raw.Time};
                    Add(pendingText);
                }
                pendingText.Keys++; pendingAt=clock.ElapsedMilliseconds;
                return;
            }
            FlushText(true);
            Step step=new Step {Target=Locate(element,raw),Milliseconds=raw.Time};
            if (raw.Kind=="leftDown")
            {
                object pattern;
                step.Kind="click";
                step.Description="ضغط: " + Label(element);
                if (!raw.Ctrl && !raw.Alt && !raw.Shift && !raw.Win && element.TryGetCurrentPattern(InvokePattern.Pattern,out pattern))
                { step.Pattern="invoke"; step.Replayable=true; }
                else if (!raw.Ctrl && !raw.Alt && !raw.Shift && !raw.Win && element.TryGetCurrentPattern(SelectionItemPattern.Pattern,out pattern))
                { step.Pattern="select"; step.Replayable=true; }
                if (String.IsNullOrEmpty(step.Target.Id) && String.IsNullOrEmpty(step.Target.Name)) step.Replayable=false;
                if (pictures && raw.Time-lastImage>=2000) { step.Picture=Capture(raw); lastImage=raw.Time; }
                downStep=step; downRaw=raw;
            }
            else if (raw.Kind=="key")
            {
                step.Kind="key";
                step.Description="مفتاح: " + (raw.Ctrl?"Ctrl+":"") + (raw.Alt?"Alt+":"") +
                    (raw.Shift?"Shift+":"") + (raw.Win?"Win+":"") + ((Keys)raw.Key).ToString() + " — خطوة يدوية";
            }
            else step.Description=raw.Kind=="scroll" ? "تمرير الشاشة؛ خطوة يدوية." : "ضغط بالزر الأيمن؛ خطوة يدوية.";
            Add(step);
        }
        private static string Label(AutomationElement element)
        {
            string name=element.Current.Name;
            return String.IsNullOrEmpty(name) ? element.Current.LocalizedControlType : name.Substring(0,Math.Min(name.Length,180));
        }
        private Locator Locate(AutomationElement element,Raw raw)
        {
            var current=element.Current;
            AutomationElement root=AutomationElement.FromHandle(raw.Window);
            string parentId="";
            try { var parent=TreeWalker.ControlViewWalker.GetParent(element); if (parent!=null) parentId=parent.Current.AutomationId; } catch { }
            return new Locator {AppPath=selected[raw.Pid].Path,Window=root.Current.Name,Id=current.AutomationId,
                Name=current.ControlType==ControlType.Edit ? "" : current.Name,
                Class=current.ClassName,ControlType=current.ControlType.Id,ParentId=parentId};
        }
        private void FlushText(bool forced)
        {
            if (pendingText==null || (!forced && clock.ElapsedMilliseconds-pendingAt<300)) return;
            try
            {
                AutomationElement focus=AutomationElement.FocusedElement; object value;
                if (saveText && Native.GetForegroundWindow()==pendingWindow && focus!=null &&
                    SameRuntime(focus.GetRuntimeId(),pendingRuntime) && !focus.Current.IsPassword &&
                    pendingElement.TryGetCurrentPattern(ValuePattern.Pattern,out value))
                {
                    var pattern=(ValuePattern)value;
                    string text=pattern.Current.Value;
                    if (text!=null && text.Length<=32768 && !pattern.Current.IsReadOnly)
                    {
                        pendingText.Value=text; pendingText.Kind="value"; pendingText.Pattern="value";
                        pendingText.Replayable=!String.IsNullOrEmpty(pendingText.Target.Id);
                        pendingText.Description="تعيين قيمة الحقل كاملة؛ راجع القيمة قبل التشغيل.";
                    }
                }
            }
            catch { }
            finally { pendingText=null; pendingElement=null; pendingRuntime=null; }
        }
        private string Capture(Raw raw)
        {
            // Optional evidence only. Never used as a click-coordinate fallback.
            if (imageCount>=40 || imageBytes>=24*1024*1024 || !SameForeground(raw)) return "";
            try
            {
                var root=AutomationElement.FromHandle(raw.Window);
                if (root.FindFirst(TreeScope.Descendants,new PropertyCondition(AutomationElement.IsPasswordProperty,true))!=null) return "";
                Native.Rect r; if (!Native.GetWindowRect(raw.Window,out r)) return "";
                Rectangle rect=Rectangle.Intersect(new Rectangle(r.Left,r.Top,Math.Max(0,r.Right-r.Left),Math.Max(0,r.Bottom-r.Top)),SystemInformation.VirtualScreen);
                if (rect.Width<1 || rect.Height<1 || rect.Width>8192 || rect.Height>8192 || (long)rect.Width*rect.Height>16000000) return "";
                if (!SameForeground(raw)) return "";
                using (Bitmap source=new Bitmap(rect.Width,rect.Height))
                using (Graphics graphics=Graphics.FromImage(source))
                {
                    graphics.CopyFromScreen(rect.Location,System.Drawing.Point.Empty,rect.Size);
                    int width=Math.Min(1280,rect.Width), height=Math.Max(1,(int)((long)rect.Height*width/rect.Width));
                    using (Bitmap scaled=new Bitmap(source,new Size(width,height)))
                    using (MemoryStream stream=new MemoryStream())
                    { scaled.Save(stream,ImageFormat.Png); imageBytes+=stream.Length; imageCount++; return Convert.ToBase64String(stream.ToArray()); }
                }
            }
            catch { return ""; }
        }
        public Session Stop()
        {
            active=false;
            ReleaseHooks(); queue.CompleteAdding();
            if (worker!=null && !worker.Join(5000))
                throw new TimeoutException("فحص أحد البرامج لم يستجب. أغلق الجلسة وأعد المحاولة بدون هذا البرنامج؛ لم يتم حفظ جلسة غير مكتملة.");
            clock.Stop();
            if (dropped>0) Manual("أحداث لم يتم تسجيلها بسبب الضغط: " + dropped,clock.ElapsedMilliseconds);
            session.DurationMilliseconds=clock.ElapsedMilliseconds;
            return session;
        }
        private void ReleaseHooks()
        {
            if (mouseHook!=IntPtr.Zero) Native.UnhookWindowsHookEx(mouseHook);
            if (keyboardHook!=IntPtr.Zero) Native.UnhookWindowsHookEx(keyboardHook);
            mouseHook=IntPtr.Zero; keyboardHook=IntPtr.Zero;
        }
        public void Dispose() { active=false; ReleaseHooks(); if (!queue.IsAddingCompleted) queue.CompleteAdding(); }
    }
    internal static class Player
    {
        internal static AutomationElement Resolve(Step step)
        {
            Locator target=step.Target;
            List<AutomationElement> windows=new List<AutomationElement>();
            foreach (Process p in Process.GetProcesses())
            {
                using (p)
                {
                    try
                    {
                        if (p.MainWindowHandle==IntPtr.Zero || !String.Equals(p.MainModule.FileName,target.AppPath,StringComparison.OrdinalIgnoreCase)) continue;
                        AutomationElement root=AutomationElement.FromHandle(p.MainWindowHandle);
                        if (root.Current.Name==target.Window) windows.Add(root);
                    }
                    catch { }
                }
            }
            if (windows.Count!=1) throw new InvalidOperationException("لا توجد نافذة مطابقة واحدة. افتح نفس النافذة والعنوان ثم أعد المحاولة.");
            var rootWindow=windows[0];
            var conditions=new List<Condition>();
            conditions.Add(new PropertyCondition(AutomationElement.ControlTypeProperty,ControlType.LookupById(target.ControlType)));
            if (!String.IsNullOrEmpty(target.Id)) conditions.Add(new PropertyCondition(AutomationElement.AutomationIdProperty,target.Id));
            if (!String.IsNullOrEmpty(target.Name)) conditions.Add(new PropertyCondition(AutomationElement.NameProperty,target.Name));
            if (!String.IsNullOrEmpty(target.Class)) conditions.Add(new PropertyCondition(AutomationElement.ClassNameProperty,target.Class));
            Condition condition=conditions.Count==1 ? conditions[0] : new AndCondition(conditions.ToArray());
            var elements=rootWindow.FindAll(TreeScope.Descendants,condition);
            List<AutomationElement> matches=new List<AutomationElement>();
            foreach (AutomationElement element in elements)
            {
                if (!element.Current.IsEnabled || element.Current.IsOffscreen || element.Current.IsPassword) continue;
                if (!String.IsNullOrEmpty(target.ParentId))
                { var parent=TreeWalker.ControlViewWalker.GetParent(element); if (parent==null || parent.Current.AutomationId!=target.ParentId) continue; }
                matches.Add(element);
            }
            if (matches.Count!=1) throw new InvalidOperationException("العنصر غائب أو له أكثر من تطابق؛ توقف التنفيذ بدون ضغط.");
            Native.SetForegroundWindow(new IntPtr(rootWindow.Current.NativeWindowHandle));
            return matches[0];
        }
        internal static void Execute(Step step,Func<bool> cancelled)
        {
            if (!step.Replayable) throw new InvalidOperationException("هذه الخطوة يدوية. نفذها بنفسك ثم اختر الخطوة التالية.");
            if (cancelled()) return;
            AutomationElement element=Resolve(step);
            if (cancelled()) return;
            object pattern;
            if (step.Kind=="value" && element.TryGetCurrentPattern(ValuePattern.Pattern,out pattern))
            {
                var value=(ValuePattern)pattern;
                if (value.Current.IsReadOnly) throw new InvalidOperationException("الحقل للقراءة فقط.");
                value.SetValue(step.Value);
                if (value.Current.Value!=step.Value) throw new InvalidOperationException("لم تتطابق قيمة الحقل بعد التنفيذ.");
            }
            else if (step.Kind=="click" && step.Pattern=="invoke" && element.TryGetCurrentPattern(InvokePattern.Pattern,out pattern))
                ((InvokePattern)pattern).Invoke();
            else if (step.Kind=="click" && step.Pattern=="select" && element.TryGetCurrentPattern(SelectionItemPattern.Pattern,out pattern))
                ((SelectionItemPattern)pattern).Select();
            else throw new InvalidOperationException("العنصر لا يدعم الإجراء المسجل.");
        }
    }
    internal sealed class MainForm : Form
    {
        private CheckedListBox applications=new CheckedListBox();
        private CheckBox textOption=new CheckBox(), pictureOption=new CheckBox();
        private Label status=new Label(), summary=new Label();
        private DataGridView grid=new DataGridView();
        private Button start, pause, finish, load, refresh, execute;
        private Recorder recorder;
        private Session session;
        private System.Windows.Forms.Timer timer=new System.Windows.Forms.Timer();
        private volatile bool cancelPlayback, playing;
        private bool unsaved;
        private Form recordingBar;
        private Label barStatus;
        private Button Button(string caption, EventHandler handler,FlowLayoutPanel panel)
        {
            Button b=new Button {Text=caption,AutoSize=true,Height=36,Margin=new Padding(5)};
            b.Click+=handler; panel.Controls.Add(b); return b;
        }
        public MainForm()
        {
            Text="بصمة شغل — نسخة تجريبية ٠٫١"; Width=1100; Height=800; MinimumSize=new Size(850,650);
            Font=new Font("Segoe UI",10); RightToLeft=RightToLeft.Yes; RightToLeftLayout=true;
            StartPosition=FormStartPosition.CenterScreen;
            var layout=new TableLayoutPanel {Dock=DockStyle.Fill,ColumnCount=1,RowCount=7,Padding=new Padding(12)};
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute,64)); layout.RowStyles.Add(new RowStyle(SizeType.Absolute,118));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute,42)); layout.RowStyles.Add(new RowStyle(SizeType.Absolute,52));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute,76)); layout.RowStyles.Add(new RowStyle(SizeType.Percent,100));
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute,64)); Controls.Add(layout);
            status.Dock=DockStyle.Fill; status.Text="اختر البرامج، ثم ابدأ تسجيل مهمة قصيرة. التسجيل متوقف.";
            status.BackColor=Color.FromArgb(232,240,246); status.Padding=new Padding(10); layout.Controls.Add(status);
            applications.Dock=DockStyle.Fill; applications.CheckOnClick=true; applications.Sorted=true; layout.Controls.Add(applications);
            var options=new FlowLayoutPanel {Dock=DockStyle.Fill};
            textOption.Text="حفظ قيم الحقول المكتوبة"; textOption.AutoSize=true;
            pictureOption.Text="لقطات للنافذة المختارة"; pictureOption.AutoSize=true;
            options.Controls.Add(textOption); options.Controls.Add(pictureOption); layout.Controls.Add(options);
            var controls=new FlowLayoutPanel {Dock=DockStyle.Fill}; layout.Controls.Add(controls);
            refresh=Button("تحديث البرامج",delegate { RefreshApps(); },controls);
            start=Button("بدء التسجيل",delegate { StartRecording(); },controls);
            pause=Button("توقف مؤقت",delegate { if(recorder!=null) recorder.TogglePause(); },controls);
            finish=Button("إنهاء وحفظ",delegate { StopRecording(); },controls);
            load=Button("فتح جلسة",delegate { LoadSession(); },controls);
            pause.Enabled=false; finish.Enabled=false;
            summary.Dock=DockStyle.Fill; summary.Text="كل التسجيلات محلية. لا يوجد تحليل سحابي أو تصوير بالكاميرا.\r\nقيم الحقول ولقطات الشاشة غير مفعلة افتراضيًا."; layout.Controls.Add(summary);
            grid.Dock=DockStyle.Fill; grid.AllowUserToAddRows=false; grid.AllowUserToDeleteRows=false;
            grid.ReadOnly=true; grid.MultiSelect=false; grid.SelectionMode=DataGridViewSelectionMode.FullRowSelect;
            grid.AutoSizeRowsMode=DataGridViewAutoSizeRowsMode.AllCells;
            grid.Columns.Add("number","رقم"); grid.Columns[0].Width=48;
            grid.Columns.Add("action","الخطوة"); grid.Columns[1].AutoSizeMode=DataGridViewAutoSizeColumnMode.Fill;
            grid.Columns.Add("ready","الإعادة"); grid.Columns[2].Width=110;
            grid.Columns.Add("keys","عدد المفاتيح"); grid.Columns[3].Width=90;
            grid.DefaultCellStyle.WrapMode=DataGridViewTriState.True;
            layout.Controls.Add(grid);
            var actions=new FlowLayoutPanel {Dock=DockStyle.Fill}; layout.Controls.Add(actions);
            execute=Button("تنفيذ الخطوة المحددة",delegate { ExecuteSelected(); },actions);
            Button("إلغاء التنفيذ",delegate { cancelPlayback=true; },actions);
            Button("عرض التفاصيل",delegate { ShowDetails(); },actions);
            Button("حفظ الجلسة",delegate { SaveSession(); },actions);
            Button("مجلد الجلسات",delegate { Directory.CreateDirectory(Storage.Folder); Process.Start(Storage.Folder); },actions);
            execute.Enabled=false;
            timer.Interval=500; timer.Tick+=delegate {
                if (recorder!=null) {
                    status.Text=(recorder.Paused?"التسجيل متوقف مؤقتًا":"التسجيل يعمل الآن")+" | خطوات: "+recorder.Count+" | "+TimeSpan.FromMilliseconds(recorder.Elapsed).ToString(@"mm\:ss");
                    status.BackColor=recorder.Paused?Color.LightGoldenrodYellow:Color.MistyRose;
                    if(barStatus!=null) {barStatus.Text=status.Text;barStatus.BackColor=status.BackColor;}
                    if (recorder.Elapsed>=30*60*1000 || recorder.Count>=10000) StopRecording();
                }
            }; timer.Start();
            Shown+=delegate { RefreshApps(); if (!Native.RegisterHotKey(Handle,1,0x4000|0x0002|0x0004,0x7B)) status.Text+=" | اختصار التوقف غير متاح؛ استخدم الأزرار."; };
            FormClosing+=OnFormClosing;
        }
        protected override void WndProc(ref Message m)
        {
            if(m.Msg==0x312 && m.WParam.ToInt32()==1) { cancelPlayback=true; if(recorder!=null) StopRecording(); }
            base.WndProc(ref m);
        }
        private void Error(Exception e) { MessageBox.Show(this,e.Message,"تعذر إكمال الخطوة",MessageBoxButtons.OK,MessageBoxIcon.Information); }
        private void RefreshApps()
        {
            applications.Items.Clear(); int own=Process.GetCurrentProcess().Id;
            foreach (Process p in Process.GetProcesses())
            {
                using(p) { try { if(p.Id!=own && p.MainWindowHandle!=IntPtr.Zero && !String.IsNullOrWhiteSpace(p.MainWindowTitle))
                    applications.Items.Add(new Target {Pid=p.Id,Path=p.MainModule.FileName,Title=p.MainWindowTitle}); } catch { } }
            }
        }
        private void StartRecording()
        {
            if(playing || recorder!=null) return;
            if(unsaved) { MessageBox.Show(this,"احفظ الجلسة الحالية أولًا."); return; }
            var targets=applications.CheckedItems.Cast<Target>().ToArray();
            if(targets.Length==0) { MessageBox.Show(this,"اختر برنامجًا واحدًا على الأقل."); return; }
            try {
                recorder=new Recorder(targets,textOption.Checked,pictureOption.Checked); recorder.Start();
                grid.Rows.Clear(); session=null; execute.Enabled=false;
                SetRecording(true); ShowRecordingBar(); WindowState=FormWindowState.Minimized;
                summary.Text="التسجيل ظاهر. توقف سريع: مفتاحا التحكم والتحويل مع المفتاح الوظيفي الثاني عشر.\r\nابدأ من البرامج المختارة، وأنهِ المهمة قبل العودة للبرنامج.";
            } catch(Exception e) { if(recorder!=null) recorder.Dispose(); recorder=null; Error(e); }
        }
        private void SetRecording(bool active)
        {
            start.Enabled=!active; refresh.Enabled=!active; load.Enabled=!active;
            pause.Enabled=active; finish.Enabled=active; applications.Enabled=!active;
            textOption.Enabled=!active; pictureOption.Enabled=!active;
        }
        private void ShowRecordingBar()
        {
            recordingBar=new Form {Text="بصمة شغل — التسجيل",Size=new Size(460,125),TopMost=true,
                FormBorderStyle=FormBorderStyle.FixedToolWindow,StartPosition=FormStartPosition.Manual,
                RightToLeft=RightToLeft.Yes,RightToLeftLayout=true};
            recordingBar.Location=new System.Drawing.Point(Screen.PrimaryScreen.WorkingArea.Right-480,20);
            barStatus=new Label {Text="التسجيل يعمل الآن",Dock=DockStyle.Top,Height=35,BackColor=Color.MistyRose};
            recordingBar.Controls.Add(barStatus);
            var row=new FlowLayoutPanel {Dock=DockStyle.Bottom,Height=48};recordingBar.Controls.Add(row);
            Button("توقف / استئناف",delegate {if(recorder!=null) recorder.TogglePause();},row);
            Button("إنهاء وحفظ",delegate {StopRecording();},row);
            recordingBar.FormClosing+=delegate(object sender,FormClosingEventArgs args) {
                if(recorder!=null) {args.Cancel=true;BeginInvoke((Action)StopRecording);}
            };
            recordingBar.Show();
        }
        private void StopRecording()
        {
            if(recorder==null) return;
            Recorder current=recorder; recorder=null;
            if(recordingBar!=null) {recordingBar.Close();recordingBar.Dispose();recordingBar=null;barStatus=null;}
            WindowState=FormWindowState.Normal;
            try { session=current.Stop(); unsaved=true; ShowSession(); SaveSession(); }
            catch(Exception e) { Error(e); }
            finally { current.Dispose(); SetRecording(false); status.BackColor=Color.FromArgb(232,240,246); }
        }
        private void ShowSession()
        {
            grid.Rows.Clear();
            foreach(Step s in session.Steps) grid.Rows.Add(s.Number,s.Description,s.Replayable?"قابلة للمحاولة":"يدوية",s.Keys==0?"":s.Keys.ToString());
            summary.Text=Analysis.Summary(session); execute.Enabled=session.Steps.Count>0;
        }
        private void SaveSession()
        {
            if(session==null || recorder!=null || playing) return;
            try { string path=Storage.Save(session); unsaved=false; status.Text="تم الحفظ: "+path; }
            catch(Exception e) { unsaved=true; status.Text="لم تحفظ الجلسة. استخدم زر الحفظ لإعادة المحاولة."; Error(e); }
        }
        private void LoadSession()
        {
            if(playing || recorder!=null) return;
            if(unsaved) { MessageBox.Show(this,"احفظ الجلسة الحالية أولًا."); return; }
            using(var dialog=new OpenFileDialog {Filter="جلسة بصمة شغل|*.wos",InitialDirectory=Storage.Folder})
            { if(dialog.ShowDialog(this)==DialogResult.OK) try { session=Storage.Load(dialog.FileName); ShowSession(); status.Text="الجلسة جاهزة للمراجعة."; } catch(Exception e) { Error(e); } }
        }
        private Step Selected()
        { return session!=null && grid.SelectedRows.Count==1 ? session.Steps[grid.SelectedRows[0].Index] : null; }
        private void ExecuteSelected()
        {
            Step step=Selected(); if(step==null || recorder!=null || playing) return;
            if(!step.Replayable) { MessageBox.Show(this,"الخطوة تحتاج تنفيذًا يدويًا. أكملها بنفسك ثم اختر التالية."); return; }
            string message=step.Description+"\r\n"+(step.Kind=="value" ? "سيتم استبدال محتوى الحقل بهذه القيمة:\r\n"+step.Value : "سيتم تفعيل العنصر داخل البرنامج الفعلي.")+"\r\n\r\nتنفيذ هذه الخطوة فقط؟";
            if(MessageBox.Show(this,message,"مراجعة خطوة",MessageBoxButtons.YesNo,MessageBoxIcon.Question)!=DialogResult.Yes) return;
            cancelPlayback=false; playing=true; execute.Enabled=false; start.Enabled=false; load.Enabled=false;
            status.Text="الخطوة ستبدأ خلال ثانيتين. يمكنك إلغاؤها.";
            var thread=new Thread(delegate() {
                string result;
                try {
                    for(int i=0;i<20 && !cancelPlayback;i++) Thread.Sleep(100);
                    if(cancelPlayback) result="تم إلغاء الخطوة.";
                    else { Player.Execute(step,delegate {return cancelPlayback;}); result=cancelPlayback?"طُلب الإلغاء؛ راجع حالة البرنامج.":"تم إرسال الإجراء. راجع نتيجة البرنامج قبل الخطوة التالية."; }
                } catch(Exception e) { result="توقف التنفيذ: "+e.Message; }
                if(!IsDisposed) try { BeginInvoke((Action)delegate {playing=false; execute.Enabled=true; start.Enabled=true; load.Enabled=true; status.Text=result;}); } catch(InvalidOperationException) { }
            }); thread.IsBackground=true; thread.SetApartmentState(ApartmentState.MTA); thread.Start();
        }
        private void ShowDetails()
        {
            Step step=Selected(); if(step==null) return;
            using(var view=new Form {Text="تفاصيل الخطوة",Width=900,Height=650,RightToLeft=RightToLeft.Yes,RightToLeftLayout=true})
            {
                string details=step.Description+"\r\n"+(step.Target==null?"":step.Target.Window+"\r\n"+step.Target.AppPath+"\r\n")+"القيمة المسجلة: "+step.Value;
                var box=new TextBox {Multiline=true,ReadOnly=true,Dock=DockStyle.Top,Height=140,Text=details,ScrollBars=ScrollBars.Vertical}; view.Controls.Add(box);
                if(!String.IsNullOrEmpty(step.Picture))
                {
                    try {
                        using(var stream=new MemoryStream(Convert.FromBase64String(step.Picture)))
                        using(var source=Image.FromStream(stream)) {
                            var picture=new PictureBox {Dock=DockStyle.Fill,SizeMode=PictureBoxSizeMode.Zoom,Image=new Bitmap(source)};
                            view.Controls.Add(picture); picture.BringToFront(); view.ShowDialog(this); picture.Image.Dispose(); return;
                        }
                    } catch(Exception e) { Error(e); }
                }
                view.ShowDialog(this);
            }
        }
        private void OnFormClosing(object sender,FormClosingEventArgs e)
        {
            cancelPlayback=true;
            if(recorder!=null) StopRecording();
            if(unsaved && MessageBox.Show(this,"الجلسة لم تحفظ. إغلاقها وفقدها؟","حفظ الجلسة",MessageBoxButtons.YesNo)!=DialogResult.Yes) {e.Cancel=true;return;}
            Native.UnregisterHotKey(Handle,1); timer.Stop();
        }
    }
    public static class App
    {
        [STAThread] public static void Main() { Run(); }
        [STAThread] public static void Run()
        {
            Native.SetProcessDPIAware(); Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false); Application.Run(new MainForm());
        }
    }
}
