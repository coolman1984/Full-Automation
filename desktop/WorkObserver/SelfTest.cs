namespace WorkObserver
{
    public static class SelfTest
    {
        private static void Assert(bool condition,string message)
        {if(!condition)throw new System.Exception("TEST FAILED: "+message);}
        public static string Run()
        {
            int checks=CoreChecks.Run();
            Session session; bool failed;
            // Exercise encryption and exact Arabic round-trip with synthetic data only.
            session=new Session();session.Steps.Add(new Step {Description="اختبار عربي",Value="بيانات تجريبية فقط"});
            string path=Storage.Save(session);
            try {
                var restored=Storage.Load(path);
                Assert(restored.Steps[0].Value==session.Steps[0].Value,"Encrypted Arabic round-trip");checks++;
                byte[] bytes=System.IO.File.ReadAllBytes(path);bytes[bytes.Length/2]^=0x40;
                System.IO.File.WriteAllBytes(path,bytes);failed=false;
                try {Storage.Load(path);} catch(System.Security.Cryptography.CryptographicException) {failed=true;}
                Assert(failed,"Modified encrypted session rejected");checks++;
            } finally {System.IO.File.Delete(path);}
            return checks+" checks passed. Native recording and replay still require a Windows UI test.";
        }
    }
}
