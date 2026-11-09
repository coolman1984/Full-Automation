using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Automation;
using System.Windows.Forms;

namespace WorkObserver
{
    internal static class DesktopNative
    {
        internal delegate bool WindowCallback(IntPtr hwnd,IntPtr parameter);
        [DllImport("user32.dll")] internal static extern bool EnumWindows(WindowCallback callback,IntPtr parameter);
        [DllImport("user32.dll")] internal static extern bool IsWindowVisible(IntPtr hwnd);
        [DllImport("user32.dll",CharSet=CharSet.Unicode)] internal static extern int GetWindowText(IntPtr hwnd,StringBuilder text,int length);
        [DllImport("user32.dll")] internal static extern bool ShowWindow(IntPtr hwnd,int command);
        [DllImport("user32.dll")] internal static extern bool SetCursorPos(int x,int y);
        [DllImport("user32.dll")] internal static extern IntPtr OpenInputDesktop(uint flags,bool inherit,uint access);
        [DllImport("user32.dll")] internal static extern bool CloseDesktop(IntPtr handle);
        [DllImport("user32.dll",CharSet=CharSet.Unicode)] internal static extern bool GetUserObjectInformation(IntPtr handle,int index,StringBuilder value,uint size,out uint needed);
        [StructLayout(LayoutKind.Sequential)] internal struct MouseInput {public int X,Y;public uint Data,Flags,Time;public UIntPtr Extra;}
        [StructLayout(LayoutKind.Sequential)] internal struct KeyboardInput {public ushort Key,Scan;public uint Flags,Time;public UIntPtr Extra;}
        [StructLayout(LayoutKind.Explicit)] internal struct InputUnion {[FieldOffset(0)]public MouseInput Mouse;[FieldOffset(0)]public KeyboardInput Keyboard;}
        [StructLayout(LayoutKind.Sequential)] internal struct Input {public uint Type;public InputUnion Value;}
        [DllImport("user32.dll",SetLastError=true)] internal static extern uint SendInput(uint count,Input[] inputs,int size);
        internal static void Send(Input input)
        {if(SendInput(1,new [] {input},Marshal.SizeOf(typeof(Input)))!=1)throw new InvalidOperationException("Windows rejected input; check the app's integrity level and desktop.");}
        internal static void Keyboard(ushort key,ushort scan,uint flags)
        {Send(new Input {Type=1,Value=new InputUnion {Keyboard=new KeyboardInput {Key=key,Scan=scan,Flags=flags}}});}
        internal static void Mouse(uint flags,uint data)
        {Send(new Input {Type=0,Value=new InputUnion {Mouse=new MouseInput {Flags=flags,Data=data}}});}
        internal static void RequireDesktop()
        {
            if(Process.GetCurrentProcess().SessionId==0)throw new InvalidOperationException("An interactive signed-in Windows session is required.");
            IntPtr handle=OpenInputDesktop(0,false,0x101);
            if(handle==IntPtr.Zero)throw new InvalidOperationException("Input desktop is unavailable or locked.");
            try {uint needed;var name=new StringBuilder(256);
                if(!GetUserObjectInformation(handle,2,name,512,out needed)||name.ToString()!="Default")
                    throw new InvalidOperationException("Unlock the desktop and close secure system dialogs first.");}
            finally {CloseDesktop(handle);}
        }
    }
    public static class WindowsBridge
    {
        private static readonly JavaScriptSerializer Json=new JavaScriptSerializer {MaxJsonLength=64*1024*1024,RecursionLimit=80};
        private static Control dispatcher;
        private static Recorder recorder;
        private static Form indicator;
        private static string lastRecording;
        private static readonly object OutputLock=new object();
        private static Dictionary<string,object> Object(object value)
        {var result=value as Dictionary<string,object>;if(result==null)throw new ArgumentException("Expected an object.");return result;}
        private static string Str(Dictionary<string,object> data,string key,string fallback="")
        {return data.ContainsKey(key)&&data[key]!=null?Convert.ToString(data[key]):fallback;}
        private static int Int(Dictionary<string,object> data,string key,int fallback=0)
        {return data.ContainsKey(key)?Convert.ToInt32(data[key]):fallback;}
        private static bool Bool(Dictionary<string,object> data,string key,bool fallback=false)
        {return data.ContainsKey(key)?Convert.ToBoolean(data[key]):fallback;}
        private static IEnumerable<object> Items(object value)
        {var list=value as IEnumerable;if(list==null||value is string)throw new ArgumentException("Expected an array.");foreach(object item in list)yield return item;}
        private static object OnUI(Func<object> action)
        {return dispatcher.Invoke(action);}
        private static string WindowTitle(IntPtr hwnd)
        {var text=new StringBuilder(2048);DesktopNative.GetWindowText(hwnd,text,text.Capacity);return text.ToString();}
        private static string ProcessPath(int pid)
        {using(var process=Process.GetProcessById(pid))return process.MainModule.FileName;}
        private static List<Dictionary<string,object>> Windows()
        {
            var windows=new List<Dictionary<string,object>>();
            DesktopNative.EnumWindows(delegate(IntPtr hwnd,IntPtr ignored) {
                try {if(!DesktopNative.IsWindowVisible(hwnd))return true;string title=WindowTitle(hwnd);if(String.IsNullOrWhiteSpace(title))return true;
                    uint pid;Native.GetWindowThreadProcessId(hwnd,out pid);
                    if(pid==(uint)Process.GetCurrentProcess().Id)return true;
                    string path="";try {path=ProcessPath((int)pid);}catch{}
                    windows.Add(new Dictionary<string,object>{{"hwnd",hwnd.ToInt64()},{"pid",pid},{"title",title},{"path",path}});
                }catch{}return true;
            },IntPtr.Zero);
            return windows;
        }
        private static IntPtr FindWindow(Dictionary<string,object> match)
        {
            if(!match.ContainsKey("hwnd")&&!match.ContainsKey("title")&&!match.ContainsKey("pid"))throw new ArgumentException("Window needs a handle, exact title or PID.");
            var found=Windows().Where(w=>
                (!match.ContainsKey("hwnd")||Convert.ToInt64(w["hwnd"])==Convert.ToInt64(match["hwnd"]))&&
                (!match.ContainsKey("pid")||Convert.ToInt32(w["pid"])==Convert.ToInt32(match["pid"]))&&
                (!match.ContainsKey("title")||Convert.ToString(w["title"])==Str(match,"title"))&&
                (!match.ContainsKey("path")||String.Equals(Convert.ToString(w["path"]),Str(match,"path"),StringComparison.OrdinalIgnoreCase))).ToList();
            if(found.Count!=1)throw new InvalidOperationException("Expected exactly one window; found "+found.Count+". Inspect windows again.");
            return new IntPtr(Convert.ToInt64(found[0]["hwnd"]));
        }
        private static Dictionary<string,object> Selector(AutomationElement element,IntPtr hwnd)
        {
            var c=element.Current;uint pid;Native.GetWindowThreadProcessId(hwnd,out pid);
            string parent="";try {var p=TreeWalker.ControlViewWalker.GetParent(element);if(p!=null)parent=p.Current.AutomationId;}catch{}
            string processPath="";try{processPath=ProcessPath((int)pid);}catch{}
            return new Dictionary<string,object>{{"window",new Dictionary<string,object>{{"title",WindowTitle(hwnd)},{"path",processPath}}},
                {"id",c.AutomationId},{"name",c.ControlType==ControlType.Edit?"":c.Name},{"class_name",c.ClassName},{"control_type",c.ControlType.Id},{"parent_id",parent}};
        }
        private static AutomationElement FindElement(Dictionary<string,object> selector)
        {
            IntPtr hwnd=FindWindow(Object(selector["window"]));
            var root=AutomationElement.FromHandle(hwnd);var conditions=new List<Condition>();
            string id=Str(selector,"id"),name=Str(selector,"name");
            if(String.IsNullOrEmpty(id)&&String.IsNullOrEmpty(name))throw new ArgumentException("Element selector needs an automation ID or an exact name.");
            if(!String.IsNullOrEmpty(id))conditions.Add(new PropertyCondition(AutomationElement.AutomationIdProperty,id));
            if(!String.IsNullOrEmpty(name))conditions.Add(new PropertyCondition(AutomationElement.NameProperty,name));
            if(selector.ContainsKey("control_type"))conditions.Add(new PropertyCondition(AutomationElement.ControlTypeProperty,ControlType.LookupById(Int(selector,"control_type"))));
            if(!String.IsNullOrEmpty(Str(selector,"class_name")))conditions.Add(new PropertyCondition(AutomationElement.ClassNameProperty,Str(selector,"class_name")));
            Condition condition=conditions.Count==1?conditions[0]:new AndCondition(conditions.ToArray());
            var matches=new List<AutomationElement>();
            foreach(AutomationElement element in root.FindAll(TreeScope.Descendants,condition)) {
                if(element.Current.IsOffscreen||!element.Current.IsEnabled)continue;
                string parentId=Str(selector,"parent_id");
                if(parentId!="") {var parent=TreeWalker.ControlViewWalker.GetParent(element);if(parent==null||parent.Current.AutomationId!=parentId)continue;}
                matches.Add(element);
            }
            if(matches.Count!=1)throw new InvalidOperationException("Expected exactly one enabled visible element; found "+matches.Count+".");
            return matches[0];
        }
        private static void FocusWindow(IntPtr hwnd)
        {
            DesktopNative.RequireDesktop();DesktopNative.ShowWindow(hwnd,9);
            if(!Native.SetForegroundWindow(hwnd)&&Native.GetForegroundWindow()!=hwnd)throw new InvalidOperationException("Windows refused foreground activation.");
        }
        private static object Snapshot(Dictionary<string,object> args)
        {
            IntPtr hwnd=FindWindow(Object(args["window"]));var root=AutomationElement.FromHandle(hwnd);
            int limit=Math.Max(1,Math.Min(1000,Int(args,"limit",300)));
            var rows=new List<object>();var queue=new Queue<AutomationElement>();queue.Enqueue(root);
            int visited=0;bool truncated=false;
            while(queue.Count>0&&visited<limit) {
                var element=queue.Dequeue();visited++;
                try {
                    var c=element.Current;if(c.IsPassword) {rows.Add(new {protected_content=true});continue;}
                    var patterns=element.GetSupportedPatterns().Select(p=>p.ProgrammaticName).ToArray();object raw;
                    string text="";if(element.TryGetCurrentPattern(TextPattern.Pattern,out raw))text=((TextPattern)raw).DocumentRange.GetText(16000);
                    string value="";if(element.TryGetCurrentPattern(ValuePattern.Pattern,out raw))value=((ValuePattern)raw).Current.Value;
                    var r=c.BoundingRectangle;
                    rows.Add(new {selector=Selector(element,hwnd),name=c.Name,type=c.LocalizedControlType,enabled=c.IsEnabled,offscreen=c.IsOffscreen,
                        text=text,value=value==null?"":value.Length>2000?value.Substring(0,2000):value,patterns=patterns,rect=new {x=Finite(r.X),y=Finite(r.Y),width=Finite(r.Width),height=Finite(r.Height)}});
                    var child=TreeWalker.ControlViewWalker.GetFirstChild(element);
                    while(child!=null&&queue.Count<limit) {queue.Enqueue(child);child=TreeWalker.ControlViewWalker.GetNextSibling(child);}
                    if(child!=null)truncated=true;
                }catch(ElementNotAvailableException){}catch(InvalidOperationException){}
            }
            return new {window=new {hwnd=hwnd.ToInt64(),title=WindowTitle(hwnd)},elements=rows,truncated=truncated||queue.Count>0};
        }
        private static double Finite(double value)
        {return Double.IsNaN(value)||Double.IsInfinity(value)?0:value;}
        private static object Screenshot(Dictionary<string,object> args)
        {
            DesktopNative.RequireDesktop();Rectangle bounds=SystemInformation.VirtualScreen;
            if(args.ContainsKey("window")) {
                IntPtr hwnd=FindWindow(Object(args["window"]));Native.Rect r;
                if(!Native.GetWindowRect(hwnd,out r))throw new InvalidOperationException("Cannot locate window.");
                bounds=Rectangle.Intersect(bounds,new Rectangle(r.Left,r.Top,Math.Max(0,r.Right-r.Left),Math.Max(0,r.Bottom-r.Top)));
            }
            if(bounds.Width<1||bounds.Height<1||(long)bounds.Width*bounds.Height>40000000)throw new InvalidOperationException("Unsupported screenshot dimensions.");
            using(var bitmap=new Bitmap(bounds.Width,bounds.Height))using(var graphics=Graphics.FromImage(bitmap))using(var stream=new MemoryStream()) {
                graphics.CopyFromScreen(bounds.Location,System.Drawing.Point.Empty,bounds.Size);bitmap.Save(stream,ImageFormat.Png);
                return new Dictionary<string,object>{{"_image",Convert.ToBase64String(stream.ToArray())},{"mimeType","image/png"},
                    {"bounds",new {x=bounds.X,y=bounds.Y,width=bounds.Width,height=bounds.Height}}};
            }
        }
        private static object ElementAction(Dictionary<string,object> args)
        {
            DesktopNative.RequireDesktop();var selector=Object(args["selector"]);var element=FindElement(selector);
            string action=Str(args,"action","invoke");object raw;
            if(action=="invoke"&&element.TryGetCurrentPattern(InvokePattern.Pattern,out raw))((InvokePattern)raw).Invoke();
            else if(action=="select"&&element.TryGetCurrentPattern(SelectionItemPattern.Pattern,out raw))((SelectionItemPattern)raw).Select();
            else if(action=="toggle"&&element.TryGetCurrentPattern(TogglePattern.Pattern,out raw))((TogglePattern)raw).Toggle();
            else if((action=="expand"||action=="collapse")&&element.TryGetCurrentPattern(ExpandCollapsePattern.Pattern,out raw)) {
                if(action=="expand")((ExpandCollapsePattern)raw).Expand();else ((ExpandCollapsePattern)raw).Collapse();
            } else if(action=="focus") {FocusWindow(FindWindow(Object(selector["window"])));element.SetFocus();}
            else if(action=="click"||action=="double_click"||action=="right_click") {
                FocusWindow(FindWindow(Object(selector["window"])));System.Windows.Point point;
                if(!element.TryGetClickablePoint(out point))throw new InvalidOperationException("Element has no clickable point.");
                Mouse(new Dictionary<string,object>{{"x",(int)point.X},{"y",(int)point.Y},{"action",action}});
            } else throw new InvalidOperationException("Element does not support requested action.");
            return new {performed=action,verified_business_result=false};
        }
        private static object Fill(Dictionary<string,object> args)
        {
            DesktopNative.RequireDesktop();var selector=Object(args["selector"]);var element=FindElement(selector);
            string text=Str(args,"text");if(text.Length>100000)throw new ArgumentException("Text exceeds limit.");object raw;
            if(element.TryGetCurrentPattern(ValuePattern.Pattern,out raw)) {
                var value=(ValuePattern)raw;if(value.Current.IsReadOnly)throw new InvalidOperationException("Field is read-only.");
                value.SetValue(text);
                if(!element.Current.IsPassword&&value.Current.Value!=text)throw new InvalidOperationException("Value verification failed.");
                return new {verified=!element.Current.IsPassword};
            }
            if(!Bool(args,"allow_keyboard"))throw new InvalidOperationException("No ValuePattern. Set allow_keyboard explicitly to type into the focused element.");
            FocusWindow(FindWindow(Object(selector["window"])));element.SetFocus();
            KeysAction(new Dictionary<string,object>{{"keys",new [] {"CTRL","A"}}});
            if(text.Length==0)KeysAction(new Dictionary<string,object>{{"keys",new [] {"BACKSPACE"}}});
            foreach(char ch in text) {DesktopNative.Keyboard(0,(ushort)ch,4);DesktopNative.Keyboard(0,(ushort)ch,4|2);}
            return new {verified=false,note="Unicode input sent; inspect the result."};
        }
        private static ushort Key(string name)
        {
            string upper=name.ToUpperInvariant();
            var aliases=new Dictionary<string,ushort>{{"CTRL",17},{"CONTROL",17},{"ALT",18},{"SHIFT",16},{"WIN",91},{"ENTER",13},{"ESC",27},{"ESCAPE",27},{"TAB",9},{"BACKSPACE",8},{"DELETE",46},{"SPACE",32},{"LEFT",37},{"RIGHT",39},{"UP",38},{"DOWN",40}};
            if(aliases.ContainsKey(upper))return aliases[upper];
            Keys key;if(!Enum.TryParse<Keys>(name,true,out key))throw new ArgumentException("Unknown key: "+name);
            int code=(int)key;if(code<1||code>255)throw new ArgumentException("Use an array of separate key names.");return (ushort)code;
        }
        private static object KeysAction(Dictionary<string,object> args)
        {
            DesktopNative.RequireDesktop();if(args.ContainsKey("window"))FocusWindow(FindWindow(Object(args["window"])));
            var keys=Items(args["keys"]).Select(v=>Key(Convert.ToString(v))).ToArray();if(keys.Length<1||keys.Length>8)throw new ArgumentException("Supply 1–8 key names.");
            var down=new List<ushort>();
            try {foreach(ushort key in keys) {DesktopNative.Keyboard(key,0,Extended(key));down.Add(key);}}
            finally {down.Reverse();foreach(ushort key in down)DesktopNative.Keyboard(key,0,2|Extended(key));}
            return new {sent=true};
        }
        private static uint Extended(ushort key)
        {return (key>=33&&key<=46)||key==91||key==92||key==111||key==144?1u:0u;}
        private static object Mouse(Dictionary<string,object> args)
        {
            DesktopNative.RequireDesktop();int x=Int(args,"x"),y=Int(args,"y");
            if(!SystemInformation.VirtualScreen.Contains(x,y))throw new ArgumentException("Mouse point is outside the virtual desktop.");
            if(!DesktopNative.SetCursorPos(x,y))throw new InvalidOperationException("Could not move pointer.");
            string action=Str(args,"action","click");
            if(action=="move")return new {moved=true};
            if(action=="wheel") {DesktopNative.Mouse(0x800,unchecked((uint)Int(args,"delta",-120)));return new {scrolled=true};}
            if(action=="drag") {
                int toX=Int(args,"to_x"),toY=Int(args,"to_y");if(!SystemInformation.VirtualScreen.Contains(toX,toY))throw new ArgumentException("Drag endpoint outside desktop.");
                DesktopNative.Mouse(2,0);
                try {for(int i=1;i<=20;i++){DesktopNative.SetCursorPos(x+(toX-x)*i/20,y+(toY-y)*i/20);Thread.Sleep(15);}}
                finally {DesktopNative.Mouse(4,0);}return new {dragged=true};
            }
            if(action!="click"&&action!="double_click"&&action!="right_click")throw new ArgumentException("Unknown mouse action.");
            for(int i=0;i<(action=="double_click"?2:1);i++) {DesktopNative.Mouse(action=="right_click"?8u:2u,0);DesktopNative.Mouse(action=="right_click"?16u:4u,0);if(i==0&&action=="double_click")Thread.Sleep(60);}
            return new {clicked=true};
        }
        private static object StopRecording()
        {
            if(recorder==null)return new {running=false,path=lastRecording};
            Recorder current=recorder;recorder=null;
            try {Session session=current.Stop();lastRecording=Storage.Save(session);return new {running=false,path=lastRecording,summary=Analysis.Summary(session)};}
            finally {current.Dispose();if(indicator!=null){indicator.Close();indicator.Dispose();indicator=null;}}
        }
        private static object StartRecording(Dictionary<string,object> args)
        {
            DesktopNative.RequireDesktop();if(recorder!=null)throw new InvalidOperationException("Recording already active.");
            var selected=new List<Target>();
            foreach(object value in Items(args["pids"])) {int pid=Convert.ToInt32(value);if(pid==Process.GetCurrentProcess().Id)throw new ArgumentException("Cannot record the bridge itself.");
                using(var process=Process.GetProcessById(pid))selected.Add(new Target {Pid=pid,Path=process.MainModule.FileName,Title=process.MainWindowTitle});}
            if(selected.Count==0)throw new ArgumentException("Choose at least one PID.");
            recorder=new Recorder(selected,Bool(args,"capture_text"),Bool(args,"screenshots"));
            try {recorder.Start();}catch{recorder.Dispose();recorder=null;throw;}
            indicator=new Form {Text="Full Automation — Recording",Size=new Size(430,115),TopMost=true,FormBorderStyle=FormBorderStyle.FixedToolWindow};
            var label=new Label {Text="التسجيل يعمل داخل البرامج المختارة",Dock=DockStyle.Top,Height=30,BackColor=Color.MistyRose};indicator.Controls.Add(label);
            var stop=new Button {Text="إنهاء وحفظ",Dock=DockStyle.Bottom,Height=35};stop.Click+=delegate {StopRecording();};indicator.Controls.Add(stop);
            indicator.FormClosing+=delegate(object sender,FormClosingEventArgs eventArgs) {if(recorder!=null){eventArgs.Cancel=true;dispatcher.BeginInvoke((Action)delegate {StopRecording();});}};
            indicator.Show();return new {running=true};
        }
        private static object Dispatch(string method,Dictionary<string,object> args)
        {
            switch(method) {
                case "windows":return Windows();
                case "snapshot":return Snapshot(args);
                case "screenshot":return Screenshot(args);
                case "focus":FocusWindow(FindWindow(Object(args["window"])));return new {focused=true};
                case "action":return ElementAction(args);
                case "fill":return Fill(args);
                case "keys":return KeysAction(args);
                case "mouse":return Mouse(args);
                case "record_start":return OnUI(delegate{return StartRecording(args);});
                case "record_stop":return OnUI(StopRecording);
                case "record_status":return OnUI(delegate{return new {running=recorder!=null,steps=recorder==null?0:recorder.Count,path=lastRecording};});
                case "record_export": {
                    Session session=Storage.Load(Str(args,"path"));foreach(var step in session.Steps)step.Picture="";return session;}
                default:throw new ArgumentException("Unknown bridge method: "+method);
            }
        }
        [STAThread] public static void Main()
        {
            Console.InputEncoding=new UTF8Encoding(false);Console.OutputEncoding=new UTF8Encoding(false);
            Native.SetProcessDPIAware();Application.EnableVisualStyles();dispatcher=new Control();IntPtr handle=dispatcher.Handle;
            var timer=new System.Windows.Forms.Timer {Interval=1000};timer.Tick+=delegate {if(recorder!=null&&(recorder.Elapsed>=1800000||recorder.Count>=10000))StopRecording();};timer.Start();
            var thread=new Thread(delegate() {
                try {string line;while((line=Console.ReadLine())!=null) {
                    object id=null;Dictionary<string,object> response;
                    try {if(line.Length>2000000)throw new ArgumentException("Request too large.");var request=Object(Json.DeserializeObject(line));id=request.ContainsKey("id")?request["id"]:null;
                        response=new Dictionary<string,object>{{"id",id},{"result",Dispatch(Str(request,"method"),request.ContainsKey("params")?Object(request["params"]):new Dictionary<string,object>())}};
                    }catch(Exception error) {response=new Dictionary<string,object>{{"id",id},{"error",error.GetBaseException().Message}};}
                    lock(OutputLock){Console.WriteLine(Json.Serialize(response));Console.Out.Flush();}
                }} finally {try{dispatcher.BeginInvoke((Action)delegate {try{StopRecording();}finally{Application.ExitThread();}});}catch{}}
            });thread.IsBackground=true;thread.SetApartmentState(ApartmentState.MTA);thread.Start();Application.Run();timer.Dispose();dispatcher.Dispose();
        }
    }
}
