# الدروس المثبتة

| الملاحظة | الإجراء المعتمد | الدليل |
|---|---|---|
| معرّف التبويب يختلف بين محاولتين | احفظ نتيجة إنشاء التبويب، واستخدم مرجعها أثناء التشغيل. احفظ منفذ الاتصال عند التسجيل | [اختبار إعادة المتصفح](../../tests/test_browser.py) |
| حقل غير ظاهر قد يترك التركيز في الحقل السابق | افحص الظهور والتركيز قبل إدخال النص | [اختبار حقل مخفي](../../tests/test_browser.py) |
| خطوة يدوية متأخرة قد تترك مهمة منفذة جزئيًا | افحص كل الفروع قبل إجراء أي خطوة أو التسجيل في المجدول | [اختبار الحاجز اليدوي](../../tests/test_core.py) |
| وجود الملف لا يثبت إن المحاولة أنشأته | احتفظ بوقت التعديل قبل العمل، واطلب ملفًا أحدث وثابتًا | [اختبار الملف القديم](../../tests/test_core.py) |
| نتيجة الإعادة وحدها لا تشرح أين فشلت محاولة سابقة | اقرأ إيصال التشغيل بترتيب الخطوات وعدد المحاولات، ثم راجع حالة التطبيق | [اختبار السجل الدائم](../../tests/test_core.py) |
| انتظار قفل الجهاز من خيط آخر يجمّد خادم الأدوات أثناء مهمة الخلفية | القفل يرفض فورًا، والوكيل يتابع بالحالة أو الإلغاء ثم يعيد المحاولة | [اختبار القفل والمهمة الخلفية](../../tests/test_core.py) |
| حقل مقارنة مكتوب خطأ ينجّح التحقق بدون حق | ارفض أي حقل غير معروف في التحقق والشرط عند الحفظ | [اختبار حقول الشرط](../../tests/test_core.py) |
| سكربت المستند الجديد وربط CDP لا يصلان للصفحة بعد التنقل | فعّل نطاقي الصفحة والتشغيل قبل الحقن والربط | [اختبار عرض كروم](../../tests/test_browser.py) |
| أي دالة يضيفها ربط CDP ظاهرة لكود الصفحة | اسم عشوائي، والسكربت ياخد الدالة ويمسحها من الصفحة، ونعتمد على معرّف سياق الصفحة الرئيسية | [اختبار التزييف](../../tests/test_browser.py) |
| `py -3` can return a broken Python whose prefix is the current folder | Probe each candidate from a neutral folder and require `Lib\os.py` under its base prefix | [Find-Python.ps1](../../scripts/Find-Python.ps1) |
| Windows PowerShell 5.1 strips double quotes from native arguments and turns native stderr into a terminating error under `Stop` | No double quotes in inline Python; relax the error preference around native probes | [Find-Python.ps1](../../scripts/Find-Python.ps1) |
| npm shims may point at an extension-less bin script | Accept it only when the shebang names node and the path stays inside the npm folder | [Shim test](../../tests/test_core.py) |
| The first `Page.navigate` on a cold Chrome exceeded the fixed 20 s transport limit | The navigation timeout governs the CDP call too | [Browser tests](../../tests/test_browser.py) |

أضف الملاحظة والقرار والاختبار الذي يعيد إنتاجها. سجّل المشاكل المتوقعة التي ما اتجرّبتش في [خطة الفجوات](../ROADMAP_AR.md).
