namespace WorkObserver
{
    public static class CoreChecks
    {
        private static void Assert(bool condition,string message)
        { if(!condition) throw new System.Exception("TEST FAILED: "+message); }
        private static Step Click(string name)
        {
            return new Step {Kind="click",Pattern="invoke",Replayable=true,
                Target=new Locator {AppPath="C:\\example.exe",Window="Test",Id=name,Name=name,ControlType=50000}};
        }
        public static int Run()
        {
            int checks=0;
            var session=new Session();
            Analysis.Validate(session); checks++;
            Assert(Analysis.RepeatedTriples(session)==0,"Empty recording"); checks++;
            session.Steps.AddRange(new [] {Click("A"),Click("B"),Click("C"),Click("A"),Click("B"),Click("C")});
            Assert(Analysis.RepeatedTriples(session)==1,"One repeated three-step pattern"); checks++;
            session.Steps[4].Replayable=false;
            Assert(Analysis.RepeatedTriples(session)==0,"Manual barrier breaks a candidate"); checks++;
            session.Steps[4].Replayable=true; session.Steps[5].Target.Window="Other";
            Assert(Analysis.RepeatedTriples(session)==0,"Different windows are different workflows"); checks++;
            session.Steps[5].Target.Window="Test"; session.Steps[5].Value="different";
            Assert(Analysis.RepeatedTriples(session)==0,"Different values are not identical work"); checks++;
            bool failed=false; session.Steps[0].Kind="shell";
            try {Analysis.Validate(session);} catch(System.IO.InvalidDataException) {failed=true;}
            Assert(failed,"Unsupported replay kinds rejected"); checks++;
            session.Steps[0].Kind="click"; session.Version=99; failed=false;
            try {Analysis.Validate(session);} catch(System.IO.InvalidDataException) {failed=true;}
            Assert(failed,"Unsupported versions rejected"); checks++;
            return checks;
        }
    }
}
