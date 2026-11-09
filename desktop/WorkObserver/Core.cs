using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
namespace WorkObserver
{
    public sealed class Target
    {
        public int Pid;
        public string Path = "", Title = "";
        public override string ToString() { return Title + "  [" + Pid + "]"; }
    }
    public sealed class Locator
    {
        public string AppPath = "", Window = "", Id = "", Name = "", Class = "";
        public int ControlType;
        public string ParentId = "";
    }
    public sealed class Step
    {
        public int Number;
        public long Milliseconds;
        public string Kind = "manual", Description = "", Value = "", Pattern = "";
        public Locator Target;
        public int Keys;
        public bool Replayable;
        public string Picture = "";
    }
    public sealed class Session
    {
        public int Version = 1;
        public string StartedUtc = DateTime.UtcNow.ToString("o");
        public long DurationMilliseconds;
        public List<Step> Steps = new List<Step>();
    }
    public static class Analysis
    {
        public static string Summary(Session session)
        {
            int ready = session.Steps.Count(s => s.Replayable);
            int apps = session.Steps.Where(s => s.Target != null).Select(s => s.Target.AppPath).Distinct().Count();
            return "الخطوات: " + session.Steps.Count + " | البرامج: " + apps +
                " | خطوات قابلة للمحاولة: " + ready + " | خطوات تحتاج تدخل: " + (session.Steps.Count-ready) +
                "\r\n" + "المدة: " + TimeSpan.FromMilliseconds(session.DurationMilliseconds).ToString(@"hh\:mm\:ss") +
                " | تكرارات محتملة: " + RepeatedTriples(session) +
                "\r\nالتحليل بقواعد محددة؛ لا يستنتج هدفك أو صحة نتيجة العمل.";
        }
        private static string Signature(Step s)
        {
            // Manual steps break candidate patterns; never infer automation from gaps.
            if (!s.Replayable || s.Target == null) return "#";
            return Encode(new [] {s.Kind, s.Target.AppPath, s.Target.Window,
                s.Target.Id, s.Target.Name, s.Target.ControlType.ToString(System.Globalization.CultureInfo.InvariantCulture), s.Target.ParentId, s.Value, s.Pattern});
        }
        private static string Encode(IEnumerable<string> values)
        { return String.Concat(values.Select(v => (v ?? "").Length.ToString(System.Globalization.CultureInfo.InvariantCulture) + ":" + (v ?? ""))); }
        public static int RepeatedTriples(Session session)
        {
            Dictionary<string, int> counts = new Dictionary<string, int>();
            for (int i = 0; i + 2 < session.Steps.Count; i++)
            {
                var parts = session.Steps.Skip(i).Take(3).Select(Signature).ToArray();
                if (parts.Contains("#")) continue;
                string key = Encode(parts);
                counts[key] = counts.ContainsKey(key) ? counts[key]+1 : 1;
            }
            return counts.Count(k => k.Value > 1);
        }
        public static void Validate(Session session)
        {
            if (session == null || session.Version != 1 || session.Steps == null || session.Steps.Count > 10000)
                throw new InvalidDataException("ملف الجلسة غير صالح أو غير مدعوم.");
            foreach (Step s in session.Steps)
            {
                if (s == null || s.Description == null || s.Value == null || s.Picture == null)
                    throw new InvalidDataException("خطوة غير صالحة.");
                if (s.Replayable && (s.Target == null || String.IsNullOrEmpty(s.Target.AppPath) ||
                    String.IsNullOrEmpty(s.Target.Window) ||
                    (s.Kind != "click" && s.Kind != "value") ||
                    (s.Kind == "click" && s.Pattern != "invoke" && s.Pattern != "select") ||
                    (s.Kind == "value" && s.Pattern != "value")))
                    throw new InvalidDataException("تعريف التنفيذ غير صالح.");
            }
        }
    }
}
