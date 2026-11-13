# سجل التغييرات

## غير منشور

- `docs/showcase/run-anatomy.html`: a self-contained, scroll-driven page that dissects one real `browser-form` run recorded over MCP stdio (187.7 ms, six steps), its failed twin (assertion refused at step 6) and the stop and fingerprint refusals. Every figure comes from `runs.sqlite3`, `actions.jsonl` or the MCP replies of that session; it states that the sample is a Linux container and a local fixture page.
- `Showcase.cmd` / `scripts/showcase.py`: a one-click end-to-end demo over MCP stdio on fixtures only (local page, throw-away Windows form). It records an agent session in headless Chrome, adds assertions, blocks a run with missing inputs, replays with new inputs, exports Python, then fills and presses a native form through UI Automation. Writes an HTML report with screenshots.
- Setup picks a healthy Python: `scripts/Find-Python.ps1` skips broken installs (for example one that prints "Could not find platform independent libraries" and takes its prefix from the current folder) instead of trusting `py -3`. Used by Setup, Connect-Claude, Connect-Codex and the Windows smoke test.
- `Build-Windows.ps1` works from PowerShell 7: it hands the build to Windows PowerShell 5.1, which has the .NET Framework compiler and UI Automation assemblies.
- Cline is detected: npm shims that point at an extension-less bin script (`node_modules\cline\bin\cline`) resolve to node plus that script, when its shebang names node and it stays inside the npm folder.
- Agent probing runs in parallel with a 45 s limit and no console window; Cline needs about 28 s to print its version behind a corporate proxy.
- `browser_navigate` passes its own timeout to `Page.navigate` instead of a fixed 20 s, so a cold first page load no longer fails early.

- الضغط والكتابة والاختيار والانتظار ورفع الملفات داخل الإطارات الداخلية من نفس الموقع. مراقبة شغلك بتسجل اللي بتعمله جوه الإطار كخطوات. الإطار من موقع تاني بيترفض بوضوح بدل التخمين، وشغلك جواه بيظهر كخطوة يدوية بدل ما يضيع.
- مقارنات أكتر في التحقق والشروط: لا يساوي، لا يحتوي، نمط نصي، أكبر من، وأصغر من (حتى لو الرقم مكتوب بفواصل زي ١٬٢٥٠).
- إعادة المحاولة بتستنى وقت بيزيد بين كل محاولة والتانية، والإيقاف والإلغاء بيفضلوا شغالين.
- الكود المُصدَّر بيرفض يشتغل لو اتعدل تعريف المهمة اللي جواه.
- مدخلات المهام المسجلة من الوكيل بقت بأسماء الحقول، والقيم التجريبية بترجع للوكيل من غير ما تتحفظ في الملف. لقطات الشاشة وقوائم التبويبات ما بقتش بتتسجل كخطوات.
- ملف سجل الأدوات بيتقسم كل ١٠ ميجا ويحتفظ بآخر ٣ ملفات، وأداة لحذف إيصالات التشغيل القديمة بقرار صريح.
- وصف لكل مدخل في الأدوات عشان الوكيل يستخدمها صح.
- اختصارات الكيبورد في عروض ويندوز بتتنفذ بعد الاستيراد في نفس النافذة، ما عدا النسخ واللصق ومفتاح ويندوز. محتاج إعادة بناء المسجل بـ `Setup.cmd`.
- سد ثغرة في مراقبة كروم: كانت الصفحة تقدر تكتشف قناة التسجيل وتضيف خطوة مزيفة، زي فتح موقع تاني، أو توقف التسجيل. القناة بقت باسم عشوائي ومخفية عن الصفحة، والإيقاف محتاج رمز سري، وأي رسالة من إطار داخلي بتتعلم كإطار.
- التعامل مع نوافذ التنبيه والتأكيد وطلب النص في كروم: قبول أو رفض تلقائي مع عرض الرسالة. الرد على نافذة أثناء مراقبة شغلك بيتسجل كخطوة.
- قراءة شات كلود كود وكودكس وكلاين اللي فتحته بنفسك ومتابعته أول بأول، بدون التفكير الداخلي وبدون ملفات تسجيل الدخول. مجلدات أنتيجرافيتي أو أي أداة تانية تتحدد في إعداد الوكيل. أمر ربط كودكس بخطوة واحدة.
- مراقبة شغلك داخل تبويب كروم وتحويله لمهمة: الضغط والضغط المزدوج والكتابة والاختيار ورفع الملفات وEnter وEscape والتنقل، بمحددات فريدة حتى داخل Shadow DOM. علامة تسجيل ظاهرة في الصفحة، ومتابعة حية للخطوات. القيم المكتوبة تبقى مدخلات بأسماء الحقول، وكلمات المرور لا تُلتقط وتتحول لمتغيرات بيئة.
- أي أمر يغيّر الجهاز يُرفض فورًا برسالة إن تشغيلًا آخر يملك الجهاز، بدل الانتظار حتى تنتهي مهمة الخلفية. كان الانتظار يجمّد خادم الأدوات كله ويمنع طلبات الحالة والإلغاء.
- حقول التحقق والشرط صارمة: `actual` مطلوب، ومقارنة واحدة فقط من `equals` أو `contains`، وأي حقل آخر يُرفض عند الحفظ. كان الحقل المكتوب خطأ يحوّل التحقق لفحص وجود قيمة فينجح بدون حق. خطوة الانتظار تقبل `seconds` فقط.
- فحص تلقائي على GitHub: التوثيق والاختبارات مع كروم حقيقي على لينكس بإصدارين من بايثون، وبناء ملفات ويندوز واختبارها الذاتي واختبارات المنطق على ويندوز.

## 0.3.0

- فحص المهام قبل التشغيل والجدولة، وعرض المدخلات الناقصة والخطوات التي قد تغيّر التطبيق. تُحجب الوصفة إذا احتوى أي فرع على خطوة يدوية.
- سجل دائم لكل محاولة وخطواتها وتوقيت التنفيذ وعدد محاولات كل خطوة، مع إلغاء مهمة في الخلفية من اتصالها المالك.
- التحقق من حداثة الملفات الناتجة واستقرارها، وفحص الحجم ووقت التعديل وبصمة المحتوى.
- تصنيف انتظار شرط جافاسكريبت كأداة يمكنها تغيير الصفحة، وإخضاعه لقواعد تشغيل الأدوات التي قد تغيّر الحالة.
- خريطة للقدرات والملفات، فهرس أدوات مولّد، ودليل تشغيل واسترجاع، وتاريخ وقرارات ودروس. خمس مهارات للمشروع بنسختين لكلود وكودكس.

## 0.2.0

- تكامل محلي بين الوكلاء وكروم وجسر ويندوز. تسجيل المهام وتصديرها وإعادة تشغيلها عبر مجدول ويندوز.
- الاحتفاظ بالمسجل البشري المشفّر، وإضافة استيراد عروضه إلى مهام قابلة للمراجعة.

## نموذج المسجل الأول

- تسجيل مرئي لنوافذ يختارها المستخدم، وحفظ عرض العمل مشفرًا، مع إمكانية إعادة الخطوات المدعومة.
