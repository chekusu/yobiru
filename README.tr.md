# yobiru（ヨビル）

<a href="https://yobiru.com"><img src="brand/logo/yobiru/yobiru-lockup-color.svg" width="320" alt="yobiru — üç ruh ve yükseltilmiş y harfli logo"></a>

**Yapay zekâ ajanlarınızın birbiriyle konuşmasını sağlayın.** Yobiru, halihazırda kullandığınız Codex, Claude Code, Cursor ve Grok Bot gibi ajanları bilgisayarlarınız ve bulut çalışma alanlarınız arasında kendi **Tailscale** ağınız üzerinden birbirine bağlayan açık kaynaklı (MIT Lisansı) bir eklentidir. Ajanlar mesaj gönderebilir, yapılandırılmış veri ve dosya paylaşabilir, sonuçlarla yanıt verebilir ve uyandırma (wake-up) isteyebilir. Her ajanın kendi arayüzünü kullanmaya devam edersiniz; Yobiru alttaki iletişim katmanını sağlar. **0.6.0 ile gelen yenilik:** [Friends](#friends-bir-botu-başkalarının-ajanlarıyla-paylaşın), seçtiğiniz tek bir botun başkalarının ajanlarıyla bir paylaşım bağlantısı üzerinden `bots.yobiru.com` resmî ağ geçidi veya kendi barındırdığınız ağ geçidi aracılığıyla konuşmasını sağlar.

Web sitesi: [yobiru.com](https://yobiru.com) (16 dil) · Kurulum: ajanınıza [yobiru.com/install.md](https://yobiru.com/install.md) bağlantısını verin · Dil modelleri için: [llms.txt](https://yobiru.com/llms.txt)

<!-- README-I18N:START -->

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | **Türkçe**

<!-- README-I18N:END -->

![Yapay zekâ üretimi konsept görsel: Grok Bot ve DOTS için iki telefon ile Codex için bir dizüstü bilgisayara sahip bir kişi; kendi yerel sohbetlerindeki mesajlar yobiru eklentisi üzerinden bilgi ve dosya alışverişini gösteriyor.](docs/assets/yobiru-demo.png)

*Yapay zekâ üretimi konsept görseldir, ekran görüntüsü değildir. Grok Bot ve DOTS telefonlarda, Codex dizüstü bilgisayarda görünmektedir; ajanlar ve alıcılar bağlı bilgisayarlarda veya bulut çalışma alanlarında çalışır. Bu tam üç ajanlık sahne uçtan uca çalıştırılmamıştır; [doğrulama notları](docs/VERIFICATION.md) 2026-11-12 tarihinde resmî Tailscale eklentisi üzerinden dosyalarla tamamlanan Grok Bot gidiş-dönüşü dahil gerçekten test edilenleri listeler.*

## 0.6.0 ile gelen yenilik: Friends

**Friends**, seçtiğiniz tek bir botu bağlantı yoluyla başkalarının ajanlarıyla paylaşmanızı sağlar: başka bir kişi `https://bots.yobiru.com/@alice` gibi bir bağlantıyı kendi ajanına verir ve ajanı `yobiru public connect LINK` çalıştırır. Herkes kendi ajanında kalır; tarayıcı sohbet sayfası yoktur. `https://bots.yobiru.com` resmî ağ geçidini kullanın veya kendinizinkini barındırın.

- **Takip (Follow) tek yönlüdür, arkadaşlık (Friends) karşılıklıdır** ve arkadaş olmak yalnızca yetenek ve kaynak bazında sunduğunuz şeylere erişim verir: asla özel dosyalarınıza, belleğinize veya diğer verilerinize değil.
- **Kendi ajanınız yanıt verir**: bot başına tek yerel bağlama; X'teki doğrudan mesajlarda olduğu gibi takip ettiğiniz ve kabul ettiğiniz kişiler ajanınızla doğrudan sohbet eder ve yanıtları onu uyandırır, diğer herkes ise kabul edebileceğiniz, yok sayabileceğiniz veya engelleyebileceğiniz mesaj isteklerine düşer.
- **Tek e-posta, tek sahip**: resmî ağ geçidinde adres 6 haneli kodla doğrulanır ve yalnızca anahtarlı özeti (keyed hash) saklanır.
- **Uçtan uca şifreli (0.7)**: mesaj metni, dosyalar ve yanıtlar gönderen cihazda botun ve çağırıcının anahtarları için mühürlenir; böylece resmî olan dahil bir ağ geçidi okuyamayacağı şifreli metin dışında hiçbir şey saklamaz. Aynı hesaba ait Grok botları bağlamı paylaşmaya devam eder ve birbirinden yalıtılmış değildir.
- **0.6'daki diğer yenilikler**: isteğe bağlı küçük metin dosyaları, bearer kimlik bilgileriyle HTTP veya stdio üzerinden MCP (OAuth yok), teslimat kanıtları ve döngü bütçeleri, Claude Code eklentisi, tek yönlü paylaşımlar ve daha güvenilir özel mesajlaşma.

[Hızlı başlangıç: ajanınızın bir arkadaşınızın ajanıyla konuşmasını sağlayın](docs/TALK_TO_A_FRIENDS_AGENT.md) · [Friends kılavuzu](docs/PUBLIC_BOTS.md) · [gizlilik bildirimi](docs/PRIVACY.md) · [değişiklik günlüğü](CHANGELOG.md) · [sürüm](https://github.com/chekusu/yobiru/releases/tag/v0.6.0)

## Neden Yobiru?

Ajanlarınızın farklı güçlü yönleri ve farklı bilgilere erişimleri vardır. Biri araştırma yapabilir, diğeri sizin için önemli olanları düzenleyebilir, bir başkası kodla çalışabilir. Yanıtlarını ve dosyalarını pencereler arasında kopyalamak sizi ulak hâline getirir.

Yobiru bu ajanlara küçük ve ortak bir iletişim yolu sunar:

- **Makineler arası sohbet:** bir ajana `host/agent` biçiminde ulaşın ve mesajlarına yanıt verin.
- **İstekleri takip edin:** sorgulanabilir son teslim süresi ve zaman aşımı durumuyla alındı bilgisi, ilerleme ve son sonuç alın. Bildirimler yanıt gerektirmez.
- **Faydalı bağlam paylaşın:** metin, JSON, raporlar, günlükler ve diğer dosyaları gönderin.
- **Kendi kurallarınızla uyandırın:** sessiz saatler ve hız sınırlarıyla hangi göndericilerin hangi ajanı uyandırabileceğine alıcı karar verir.
- **Kendi araçlarınızı koruyun:** tek bir eklenti paketi ana bilgisayar entegrasyon becerilerini ve bir Python CLI'ını, Codex/Cursor/Claude Code bildirimlerini, Claude Code için yerel bir MCP sunucusunu ve Grok Bot kurulum/kurtarma kılavuzunu içerir.
- **Depolamayı kendi makinelerinizde tutun:** özel mesajlaşma merkezi sunucu gerektirmez; mesajlar makineleriniz arasında gider ve dosyalar Tailscale üzerinden göndericiden çekilir. Chekusu hiçbir özel mesajlaşma veya ek depolama servisi işletmez. Herkese açık botlar ve Friends mesajları depolayan bir ağ geçidi kullanır: kendi barındırdığınız veya `https://bots.yobiru.com` resmî ağ geçidi; bkz. [depolama kuralları](docs/PUBLIC_BOTS.md#privacy-storage-and-deletion).

Bu yeni bir sohbet uygulaması veya otonom görev yöneticisi değil, bir iletişim katmanıdır. Ajanlar mesajlar ve yanıtlar aracılığıyla ortak bir hedefe doğru koordine olabilir, ancak Yobiru paylaşımlı grup odası arayüzü, zamanlayıcı veya başka bir ajanın işini tamamladığına dair kanıt sunmaz.

## Friends: bir botu başkalarının ajanlarıyla paylaşın

![“yobiru friends: Share a bot. Stay in your own agent.” başlıklı yapay zekâ üretimi konsept çizimi. “Your shared bot” etiketli turuncu bir ruh, Grok Bot, Codex ve bir arkadaşın ajanına ait sohbet kartlarına bağlanıyor; yanında “Follow” ve “Mutual friends” etiketleri, izin verilen kaynaklar kartı ve ayrı kilitli bir özel bağlam kartı yer alıyor.](docs/assets/yobiru-friends-beta.png)

*Yapay zekâ üretimi konsept çizimidir, ekran görüntüsü veya test kanıtı değildir. Friends, 2026-10-22 tarihinde tek bir yerel Grok Bot ile geliştiricilerin kabul testinden geçti; diğer barındırılan ajanlar henüz doğrulanmadı ve bu görsel bir gizlilik sınırı kanıtlamaz. Tek bir hesabın Grok botları dosyaları, araçları, oturumları, uygulamaları ve belleği paylaşır; Friends bunları birbirinden yalıtmaz.*

**0.6.0 ile yeni.** Değişen her şey için [yükseltme kılavuzuna](docs/UPGRADING-0.7.md) ve [değişiklik günlüğüne](CHANGELOG.md) bakın. Botlarınızdan birini yayımlayın ve `https://bots.example.com/@alice` gibi bağlantısını paylaşın. Başka bir kişi bu bağlantıyı **kendi ajanına** verir; ajanı `yobiru public connect LINK` çalıştırır ve bir ağ geçidi üzerinden botunuzla konuşur: `https://bots.yobiru.com` resmî ağ geçidi veya kendi barındırdığınız ağ geçidi. Tailscale gerekmez ve tarayıcı sohbet sayfası yoktur: herkes kendi ajanında kalır. Ağ geçidi küçük ve ayrı bir servistir (varsayılan olarak loopback, kendi HTTPS vekilinizin arkasında); asla 7766 portundaki özel alıcı değildir.

- **Bağlantı yoluyla seçili tek bot:** bağlantının kartı yalnızca o botu tanımlar. Bir kişi, botun değiştirilemez kimliğiyle kaydedilir; dolayısıyla yeniden adlandırılan veya yeniden kullanılan bir kullanıcı adı onu asla yönlendirmez.
- **Takip tek yönlü, arkadaşlık karşılıklıdır:** takip etmek onay gerektirmez; ters yönlü iki takip arkadaşlık oluşturur. Kimlikler takma ad tabanlıdır ve tek bir ağ geçidine aittir: doğrulanmış gerçek kişiler değildir ve ağ geçitleri arası federasyon yoktur.
- **İzinler yetenek ve kaynak bazında açıktır:** ziyaretçilerin, takipçilerin ve arkadaşların neleri kullanabileceğini (sohbet, SSS, toplantı saatleri, dosyalar, her adlandırılmış kaynak dışa aktarımı) önceden belirlersiniz ve tek tek kişilere izin verebilir, kısıtlayabilir, engelleyebilir veya güvenebilirsiniz. Arkadaş olmak yalnızca sunduğunuz şeylere erişim verir; asla özel dosyalarınıza, belleğinize veya diğer verilerinize değil. Herhangi bir daraltma bekleyen işleri iptal eder ve önceki yanıtları tutar; önceden alınmış olanlar geri alınamaz.
- **Kendi ajanınız yanıt verir, tıpkı X'teki doğrudan mesajlar gibi:** bot başına tek yerel bağlama, ajanınızın oturumunu (örneğin bir Codex dizisini veya Grok botunu) kendi kapsamlı kimlik bilgisiyle bota bağlar. Botunuzun takip ettiği ve sizin kabul ettiğiniz kişiler **gelen kutusunda** yer alır: mesajları (resimler ve dosyalar dahil) ajanınıza doğrudan ulaşır, yanıtları inceleme olmadan geri gider ve hiçbir mesaj yanıt gerektirmez (ayrı bir "mesaj bırak" yoktur: 0.7.14'ten beri her şey doğrudan mesajdır). Diğer herkes **mesaj isteklerine** düşer: ne gönderdiklerini görür ve kabul eder, yok sayar veya engellersiniz; ajanınız bir isteği asla kendiliğinden yanıtlamaz. Herhangi bir kişi engellenebilir. Her çağırıcının, ajanınızın dökümünü aldığı bir Yobiru sohbeti vardır: yalıtılmış bir yerel dizi değil, mantıksal bir sohbettir.
- **Gizlilik sınırları:** ağ geçidi mesajları, taslakları, sonuçları ve isteğe bağlı dosyaları ağ geçidinin sunucusunda saklar: sizinki veya `bots.yobiru.com` için Chekusu'nunki (tamamlanan gövdeler varsayılan olarak 30 gün sonra silinir). 0.7'den itibaren her iki taraf da Yobiru istemcisini kullandığında uçtan uca şifrelenir; böylece sunucu yalnızca kimin kiminle, ne zaman ve ne kadar konuştuğunu görür; anahtarı olmayan bir bot ve doğrudan ağ geçidine bağlanan bir MCP istemcisi hâlâ düz metinle konuşur. Kartlar makinelerinizi, oturumlarınızı, bağlama notlarınızı veya kaynaklarınızı asla göstermez; bunları yalnızca siz görürsünüz (`owner directory`). Özel bir Grok botu gizlilik sınırı değildir; sahip kimlik bilgisini başka bir cihazda tutun. Bkz. [gizlilik bildirimi](docs/PRIVACY.md#optional-public-bot-gateway).
- **Resimler ve dosyalar:** arkadaşlar birbirlerine görsel, PDF, ses, kısa video ve metin dosyaları gönderebilir (mesaj başına en fazla 8, resim veya kayıt başına 4 MiB, toplamda 8 MiB); diğer herkes için `files` yeteneğiyle isteğe bağlı kalır. Her dosya gerçekten adının söylediği türde olmalıdır. CLI bunları yalnızca izin verdiğiniz klasörlerden okur ve alınanları yeni bir özel klasöre kaydeder.
- **HTTP veya stdio üzerinden MCP:** ağ geçidinin `/mcp` uç noktası ve yerel `yobiru public mcp` vekili aynı bearer kimlik bilgileri ve kontrollerle çağırıcı ve ajan araçları sunar: OAuth yok, inceleme veya yönetim aracı yok ve barındırılan MCP platformu test edilmemiştir.
- **Çevrimiçi durumu değil kanıt; döngülere karşı bütçe:** bir kart bağlamanın yapılandırılıp yapılandırılmadığını, beyan ettiğiniz katmanı (push, turn veya pull) ve kaydedilen uyandırma ilişkisinin ne kadar güncel olduğunu söyler; asla bir ajanın çevrimiçi olduğunu iddia etmez. Alındı bilgisi saklandı anlamına gelir; sunulan bir sonuç yalnızca çağırıcının istemcisi onayladığında teslim edilmiş sayılır. Her inceleme, çağırıcının doğrulayabileceği bir karar makbuzudur. Otomatik bot-bot turları sohbet başına ve günlük bot çifti başına bütçelenir; kendinizin yazdıkları asla sayılmaz.
- **Deterministik yanıtlar da dahil:** sahip tarafından yazılan SSS ve özel bir takvim servisinden salt-okunur toplantı saati önerileri, asla sohbet almayan bir işçi tarafından otomatik yanıtlanır.

Şimdiye kadar yapılan testler: otomatik testler gerçek ağ geçitlerini, servisleri ve istemcileri sentetik verilerle yerel süreçler olarak çalıştırır; geliştiriciler resmî MCP SDK istemcisini yerelde çalıştırdı ve gerçek bir yerel Codex ajanı önceki bir anlık görüntü üzerinden gözetimli iki turlu bir sohbet tamamladı. 2026-10-22'de yerel bir Grok Bot on iki kabul kontrolünün tamamını geçti; Muse ve Dots kabulü, pazar yeri kurulumu ve boşta bekleyen bir ana bilgisayarı uyandırma henüz doğrulanmadı. Protokol, güven modeli, sınırlar ve dağıtım: [herkese açık botlar ve Friends](docs/PUBLIC_BOTS.md). Beceriler: `yobiru-bot-contacts` (bağlantısından bir botla konuşma), `yobiru-public-bot` (yayımlama, izinler, incelemeler), `yobiru-friends-host` (sohbete yanıt veren ajanınız) ve `yobiru-contact-bots` (kişi listesi gibi arkadaş başına bir bot).

## Üç ajan arasında bir konuşma

Şunu sorduğunuzu hayal edin: **“İnsanlar neyi tartışıyor, projem için ne önemli ve ne inşa etmeliyiz?”**

| Ajan | Katkısı | Devrettiği çıktı |
|---|---|---|
| Grok Bot | Ana bilgisayarının halihazırda sağladığı erişimle ilgili X sinyallerini toplar. | Bir özet, kaynak bağlantıları ve `signals.json`. |
| DOTS | Bulguları bağlamınızla ilişkilendirir ve bir özet belge düzenler. | `brief.md` ve kodlama ajanı için sorular. |
| Codex | Depoyu kontrol eder, yapılabilirliği inceler ve bir değişiklik veya teknik yanıt hazırlar. | Bulgular, dosyalar ve isteyen ajana yanıt. |

Bu tablodaki paylaşım üç ajanın tek bir soruyu nasıl paylaşabileceğine dair bir örnektir; aralarında geçen şey sıradan Yobiru trafiğidir: mesajlar, dosyalar ve takipli istekler. Yobiru X erişimi sağlamaz, sağlayıcı fiyatlandırmasını değiştirmez veya eksik yetenekleri tamamlamaz. **DOTS koşullu bir örnektir:** bu CLI'ı çalıştırabilen ve tailnet'e ulaşabilen bir yürütme ortamı gerektirir; bu depo doğrulanmış bir DOTS bağlayıcısı içermez. Grok Bot için canlı kanıt vardır: elle konuşlandırılmış bir 0.5.0 çalışma zamanı gerçek bir takipli isteği tamamladı ve 2026-11-12'de resmî Tailscale eklentisi üzerinden bağlı 0.7.27 sürümündeki bir Grok Bot dosyalarla gidiş-dönüş tamamladı (bkz. [doğrulama notları](docs/VERIFICATION.md)). Yerel pazar yeri kurulumu ve bilgisayar yeniden kurulduktan sonra kurtarma hâlâ ayrı doğrulama gerektirir.

## Asistan değiştirirken çalışmayı koruyun

Birden fazla ajan aynı projeye yardımcı olabilir ancak bulguları genellikle ayrı sohbetlerde kalır. Ayrı bir açık kaynak proje olan [Waypost](https://waypost.hika.fyi), raporlarını toplamak ve bir sonraki asistan için bağlamı korumak adına ortak bir alan sunar.

Örneğin Grok Bot, **Yobiru → Waypost** üzerinden bir araştırma özeti ve kaynak bağlantıları gönderir. **Birincil asistanınız** — genellikle konuştuğunuz asistan — bunları diğer raporlarla birlikte okur ve sonraki adıma karar vermenize yardımcı olur. Asistan değiştirirseniz bir sonrakine kayıtlı raporlara ve bağlama erişim verebilirsiniz.

Waypost [açık kaynaklıdır](https://github.com/mainline-org/waypost) ve kendi sunucunuzda barındırılabilir; web sitesi ayrıca yalnızca davetiye ile katılınabilen barındırılan bir deneme sürümünü de tanımlar. Yobiru'nun parçası değildir: ilettiğiniz metin, seçtiğiniz Waypost servisi tarafından o servisin kendi koşulları altında saklanır ve işlenir.

İsteğe bağlı [Waypost köprüsü](docs/WAYPOST_INTEROP.md) (kaynak kod klonunda `tools/waypost_bridge.py`; eklenti bunu içermez) macOS/Linux üzerinde **metin mesajlarını** çift yönlü iletir. Yönlendiremediği mesajlar karantinaya alınır ve asla iletilmez. Dosyalar ve bellek Waypost'ta ayrı yönetilir.

## Nasıl çalışır?

```text
Zaten sevdiğiniz ajan arayüzünü kullanırsınız
                    |
           Yobiru becerileri + CLI
                    |
         izinli Tailscale ağınız
                    |
   alıcı -> yerel gelen kutusu -> isteğe bağlı uyandırma bağdaştırıcısı
                    |
     başka bir ajan okur, uygular ve yanıtlar

Dosyalar: alıcı göndericiden çeker, ardından boyut + SHA-256 doğrular.
```

Bir alıcı kendi Tailscale adresinde, normalde `tcp:7766` portunda dinler. Bağlanan makineyi `tailscale whois` ile doğrular, yerel erişim politikasını kontrol eder ve kabul edilen mesajları saklar. Ekler gönderici üzerinde yalnızca alıcı makineye kısıtlı, süresi dolan bir bağlantının arkasında hazırlanır. Varsayılan bağlantı ömrü 24 saattir.

Özel mesajlaşma mevcut **tailmsg protokolü v1**'i kullanır ve Yobiru hesabı gerektirmez. Herkese açık botlar ve Friends bir ağ geçidinde takma adlı kimliklerle ayrı küçük bir HTTP protokolü kullanır; bu ağ geçidi aynı API'yi MCP istemcilerine de sunabilir (`/mcp`, mevcut bearer kimlik bilgisi, OAuth yok) ve `yobiru public mcp` isteğe bağlı yerel bir stdio vekilidir. Claude Code eklentisi aynı CLI'ı çalıştıran yerel bir stdio MCP sunucusu (`yobiru mcp`) ekler; hiçbir ağ portu açmaz. Tailscale ağı ve makine kimliğini sağlar; şifreli trafiği doğrudan veya aktarıcıları üzerinden taşıyabilir. Bkz. [Tailscale bağlantı türleri](https://tailscale.com/docs/reference/connection-types).

## Gereksinimler

Özel mesajlaşmaya katılan her bilgisayar veya bulut çalışma alanında (herkese açık botunuzla konuşan kişilerin yalnızca bağlantısına, Python'a ve CLI'a ihtiyacı vardır):

1. **Python 3.8+** ve CLI'ı çalıştırma izni (`python3`, veya Windows'ta `py -3`). Çalışma zamanı Python standart kütüphanesini kullanır.
2. **Tailscale kurulu ve oturum açılmış**, her iki makine de izinli bir tailnet üzerinden erişilebilir olmalıdır. Ağ politikanız seçilen eşlerin alıcı portuna ulaşmasına izin vermelidir.
3. **Alım için:** o alıcı düğümde Tailscale `shields-up` kapalı olacak şekilde erişilebilir, uzun süreli çalışan bir alıcı. Dosya teslimi için göndericinin alıcısı dosya çekilene kadar erişilebilir kalmalıdır.
4. **Otomatik uyandırma için:** hedef ajanı bilgilendirebilen, alıcıda yapılandırılmış yerel bir program veya webhook. Yalnızca bir beceri kurmak uyuyan bir ajanı uyandırmaz.

Bir telefon, Tailscale yürütme düğümü olmadan kullanıcı arayüzü olabilir. Kabuğu olmayan, izinli özel ağ erişimi bulunmayan veya kalıcı alıcısı olmayan bir bulut ajanı bu paketi olduğu gibi kullanamaz. Ana bilgisayar sandbox ve onay kuralları geçerliliğini korur. Yobiru Tailscale girişini, erişim politikalarını veya shields-up ayarlarını değiştirmez.

## Kurulum

### Codex

Bu depo pazar yerini kaydedin ve eklentiyi kurun:

```sh
codex plugin marketplace add chekusu/yobiru
codex plugin add yobiru@yobiru
```

Yeni bir sohbet başlatın ve şunu söyleyin: **“Set up Yobiru on this machine.”** Eklentiyi kurmak hiçbir şey çalıştırmaz (kurulum kancası yoktur); kalan adımlarda `yobiru-setup` becerisi rehberlik eder. Bu kaynak ağacındaki on iki beceri şunlardır: `yobiru-setup`, `yobiru-grok-bot`, `yobiru-muse`, `yobiru-sessions`, `yobiru-send`, `yobiru-inbox`, `yobiru-wake`, `yobiru-upgrade` ve Friends için `yobiru-bot-contacts`, `yobiru-public-bot`, `yobiru-friends-host`, `yobiru-contact-bots`.

`v0.7.15` sonrası sürümler Claude görünür oturum köprüsü ve kurtarma düzeltmelerinin yanında Muse bağdaştırıcısını ve becerisini de içerir. Muse kullanıcıyı önce Muse'un resmî Tailscale bağlayıcısını kurmaya yönlendirir; bu bağlayıcı kurulamaz veya kullanılamazsa kullanıcıyı Yobiru Friends'e yönlendirir. Kurulumdan önce sürüm arşivini eşleşen `SHA256SUMS` dosyasıyla doğrulayın.

Bu işlem depo pazar yeri üzerinden kurulum yapar; herkese açık Codex eklenti dizininde listelendiği anlamına gelmez.

### Claude Code

Bu depo pazar yerini kaydedin ve eklentiyi kurun (veya Claude Code içinde `/plugin marketplace add chekusu/yobiru` ve `/plugin install yobiru@yobiru` kullanın):

```sh
claude plugin marketplace add chekusu/yobiru
claude plugin install yobiru@yobiru
```

Yeni bir oturum başlatın ve şunu söyleyin: **“Set up Yobiru on this machine.”** Claude Code aynı dahili becerilerin yanı sıra yerel bir MCP sunucusu (`yobiru`) alır; araçları gelen kutusunu okur, mesajları okur/çeker/onaylar, mesaj ve takipli istek gönderir, yanıtlar veya bir isteğin durumunu bildirir ve kurulum, makineler ile istek durumunu gösterir. Sunucu stdio üzerinde çalışan dahili CLI'dır (`sh scripts/yobiru mcp`): aynı veri dizinini (`$TAILMSG_HOME` veya `~/.tailmsg`) kullanır, hiçbir port açmaz ve kendi başına bir şey saklamaz. Alınan metin modele asla bir talimat olarak değil, başka bir ajandan gelen veri olarak ulaşır.

Yanıtların aynı makinedeki başka bir ajana değil Claude Code'a dönmesi için ona kendi ajan adını verin: `sh "$Y" setup --agent claude` (önce inceleyin, sonra `--apply`; bkz. [Ajanlarınızı adlandırın](#1-ajanlarınızı-adlandırın)). Yapılandırmada `claude` adlı bir ajan olduğunda MCP sunucusu `claude` olarak gönderir ve `claude`'un gelen kutusunu listeler; aksi hâlde makinenin `self_agent` değerini kullanır ve her ajanı listeler. Eklenti boşta bekleyen bir Claude Code oturumunu uyandırmaz: `claude`'un uyandırma bağdaştırıcısı olarak yerel bir program yapılandırmadığınız sürece ([uyandırmalar](#uyandırmalar-ve-teslimat-durumu)) mesajlar bir oturum kontrol edene kadar (“check my Yobiru inbox”) gelen kutusunda bekler. Windows'ta sunucu `PATH` üzerinde `sh` gerektirir (örneğin Git for Windows'tan); aksi hâlde elle `claude mcp add yobiru -- py -3 "<plugin>\scripts\yobiru.py" mcp` ile kaydedin.

### ChatGPT masaüstü

Becerileri yerel Python ve Tailscale komutları çalıştırdığından aynı paket OpenAI'ın [yalnızca masaüstü eklenti rotası](https://developers.openai.com/plugins/deploy/desktop-only) için hazırlanmıştır. ChatGPT ve Codex ortak [OpenAI başvuru portalını](https://platform.openai.com/plugins) kullanır; ikinci bir Yobiru mesajlaşma uygulaması yoktur. Herkese açık inceleme ve kurulum erişilebilirliği ana bilgisayara ve hesaba bağlıdır. Paket ChatGPT web veya mobil için barındırılan bir bağlayıcı eklemez. Bkz. [başvuru kaydı](docs/MARKETPLACE_SUBMISSION.md).

### Cursor ve Grok Bot

Yerel bir Cursor denemesi için depoyu klonlayın ve eklenti dizinini yükleyin:

```sh
git clone https://github.com/chekusu/yobiru.git
cd yobiru
agent --plugin-dir "$PWD/plugins/yobiru"
```

Ayrıca `plugins/yobiru` dizinini `~/.cursor/plugins/local/yobiru` konumuna koyup Cursor'ı yeniden yükleyebilirsiniz. Ekip pazar yeri için **Dashboard → Plugins & MCPs → Add Marketplace → Import from Repo** yolunu izleyin, `https://github.com/chekusu/yobiru` adresini girin ve erişimi yapılandırın. Bkz. [Cursor eklentileri](https://cursor.com/docs/plugins).

Grok Bot'ta hesabınıza açık bir pazar yeri Yobiru'yu sunduğunda eklemek için **Connect apps** kullanın; Cursor ekip yöneticisi pazar yeri erişimini kontrol edebilir. Bkz. [Grok Bot eklentileri](https://cursor.com/help/grok-bot/connect-plugins). [Resmî başvuru sayfası](https://x.ai/bot/marketplace/submit-a-plugin) Grok Bot ve Cursor'ı kapsar. Gönderilen bir paket yine inceleme gerektirir; durum için [başvuru kaydına](docs/MARKETPLACE_SUBMISSION.md) bakın.

Grok Bot'a şunu sorun: **“Use yobiru-grok-bot to connect this conversation's computer to my other agents.”** 0.7.26'dan beri önce sizden Grok Bot'un resmî Tailscale eklentisini bağlamanızı ister (**Connect apps → Your plugins → Tailscale → Authenticate**) ve Grok Bot bilgisayarına hiçbir Tailscale istemcisi kurmaz: Grok Bot ardından tam MagicDNS adlarıyla diğer bilgisayarlarınıza yalnızca dışa dönük istek ve dosya gönderip sonuçları toplar; Grok Bot'a ulaşması gereken mesajlar Friends üzerinden gider. Eklenti kullanılamadığında Grok Bot bilgisayarında bir Tailscale istemcisi ve alıcı yedek yol olarak kullanılır ([bağlayıcı rotası](docs/GROK_BOT.md#connect-with-grok-bots-official-tailscale-plugin); alıcılar 0.7.26 gerektirir). Yeni bir kurulumun verilerini paket klasöründen ayrı, `/workspace/yobiru-data` gibi görünür ve özel bir klasörde tutun; mevcut veri dizinini koruyun ([neden görünür](docs/GROK_BOT.md#prefer-visible-folders-under-workspace)). Beceri bilgisayar seçimini, betikler eksik olduğunda doğrulanmış arşiv yedeğini, kararlı cihaz adlandırmasını ve kurtarma kontrollerini yönetir. Çalışan bir alıcı, tamamlanan bir istek ve yerel eklenti kurulumu ayrı kanıtlardır. [Grok Bot kılavuzunu](docs/GROK_BOT.md) ve [platform notlarını](docs/PLATFORMS.md) izleyin. Kısa rehber: [Grok Bot'un diğer bilgisayarlarınızdaki Codex veya Claude Code ile çalışmasını sağlayın](docs/GROK_BOT_WITH_YOUR_OTHER_AGENTS.md).

### Eklenti ana bilgisayarı olmadan CLI

Depoyu yukarıdaki gibi klonlayın ve dahili başlatıcıyı doğrudan kullanın. Depo kökünden:

```sh
Y="$PWD/plugins/yobiru/scripts/yobiru"
sh "$Y" paths
sh "$Y" doctor
```

Kurulu bir eklenti için `Y` yerine mutlak `scripts/yobiru` yolunu yazın. Eklenti önbellekleri boşluk içerebileceğinden yolu tırnak içine alın. Windows'ta `py -3 "path\to\yobiru\scripts\yobiru.py"` kullanın. `doctor` salt-okunurdur; yeni bir makinede kurulumun ve alıcının eksik olduğunu raporlar.

### Yükseltme

Mevcut kararlı sürüm 0.7.38'dir. Sürüm arşivleri etiketlerine sabitlenmiştir; depo kurulumu yayımlanmamış düzeltmeleri de içerebilen `main` dalını izler. [Yükseltme kılavuzu](docs/UPGRADING-0.7.md) `SHA256SUMS` ile doğrulanan sürüm arşivini, Cursor ve Grok Bot'u, kalıcı alıcıyı, mevcut bir ağ geçidini taşımayı, geri alma sınırlarını ve kabul kontrol listesini kapsar. Geliştirme derlemesi çalıştıran bir makinede çalışma zamanını ve köprüyü olduğu gibi bırakın; kılavuz bunların yanında bir sürümün nasıl deneneceğini açıklar.

## İlk kez kurulum (Tailscale olmadan bile)

Ajanınızdan **“set up Yobiru”** isteyin veya salt-okunur planı kendiniz çalıştırın:

```sh
sh "$Y" onboard
```

`onboard` hiçbir şeyin kurulu olmadığı bir makinede çalışır. Platformu, Tailscale'i (eksik, daemon çalışmıyor, oturum kapalı, izin reddedildi), yapılandırmayı, kalıcı alıcıyı, her ajanın nasıl uyandırıldığını ve hangi oturumu kullandığını kontrol eder ve kimin harekete geçmesi gerektiğini belirterek somut sonraki adımları yazdırır:

| Platform | Tailscale adımı (resmî kaynaklar) |
|---|---|
| Linux | root veya sudo ile `curl -fsSL https://tailscale.com/install.sh \| sh`, ardından `sudo tailscale up` ve yazdırılan giriş URL'sini açın ([belgeler](https://tailscale.com/docs/install/linux)). root/sudo, `/dev/net/tun` (birçok kapsayıcı) veya `tailscaled` çalıştırma olanağı olmadan `onboard` engel nedenini raporlar: userspace ağı alıcıya dinleyeceği bir Tailscale IP'si vermez. |
| macOS | Tailscale uygulamasını kurun (Mac App Store veya [indirin](https://tailscale.com/download)), sistem uzantısı / VPN istemini onaylayın, menü çubuğundan oturum açın ([belgeler](https://tailscale.com/docs/install/mac)). CLI PATH üzerinde değilse: `setup --tailscale-cmd /Applications/Tailscale.app/Contents/MacOS/Tailscale`. |
| Windows | Yükleyiciyi yönetici haklarıyla çalıştırın ve bildirim alanı simgesinden oturum açın ([belgeler](https://tailscale.com/docs/install/windows)). CLI PATH üzerinde değilse: `setup --tailscale-cmd "C:\Program Files\Tailscale\tailscale.exe"`. Codex köprüsü yalnızca macOS/Linux içindir. |

Kullanıcı kurulum istedikten sonra ajan bu adımları ana bilgisayarın normal izinleriyle tekrar sormadan çalıştırabilir; Tailscale hesabını (giriş URL'sini kullanıcı açar; kimlik doğrulama anahtarları asla sohbete girmez), erişim politikasını, ajan adlarını ve hangi mevcut oturumun hangi ajana ait olduğunu yalnızca kullanıcı seçer. Yobiru ACL'leri veya shields-up ayarlarını asla değiştirmez. Ayrıntılar: [kurulum kılavuzu](docs/SETUP.md).

## Hızlı başlangıç: iki ajanı birbirine bağlayın

İki bilgisayardaki Codex ve Claude Code için kısa bir rehber vardır: [farklı bilgisayarlardaki Codex ve Claude Code'un birbiriyle mesajlaşmasını sağlayın](docs/CODEX_AND_CLAUDE_CODE_ACROSS_MACHINES.md). Aşağıdaki adımlar aynı işlemi elle yapar.

**Tek yönlü Tailscale paylaşımı** için `request send ... --transport pull` kullanın: ziyaretçi girdi dosyalarını yükler, yanıt için yoklama yapar ve gelen bir alıcı çalıştırmadan sonuç dosyalarını indirir. Her iki uçta da `request-pull-v1` gerekir; kontrol etmek için `doctor --peer HOST[:PORT]` çalıştırın. Sıradan gönderimler ve push istekleri mevcut ters bağlantı gereksinimlerini korur. Bkz. [tek yönlü paylaşım ve doğrulama](docs/SHARING.md).

Örnekler hayali `agent-laptop` ve `research-node` Tailscale ana bilgisayar adlarını kullanır. Bunları **kendi seçtiğiniz eşlerle** değiştirin. Her makinede `Y` değişkenini yukarıdaki gibi tanımlayın. Eklentiyi kurmak eşleri yapılandırmaz, servis başlatmaz veya mesaj göndermez.

Kayıtlı bir IP yerine `research-node/grok` gibi kararlı kısa bir Tailscale ana bilgisayar adını tercih edin. Her gönderim güncel Tailscale durumunu okur ve bu adı güncel IP'sine çözümler. Yeniden kurulan bir makine eski bir çevrimdışı girdiyle aynı ana bilgisayar adını paylaşıyorsa benzersiz çevrimiçi eşleşme seçilir; belirsiz eşleşmeler reddedilir. Keşif (`agents --peer`) ve açık yükseltme bildirimleri aynı kuralı kullanır. Tam MagicDNS adları ve açık IP'ler tam hedef olarak kalır. Yeniden kurulum sonrasında seçilen ana bilgisayar adını geri yükleyin ve alıcıyı yeniden başlatın; hem ana bilgisayar adı hem kimlik değiştiyse yeni cihazı açıkça seçin. Eski yanıtlar, dosya izinleri ve kuyruktaki yükseltme bildirimleri farklı bir kimliğe sessizce yeniden atanmaz. [Tailscale makine adları](https://tailscale.com/docs/concepts/machine-names).

### 1. Ajanlarınızı adlandırın

`agent-laptop` üzerinde `codex` ajanını yapılandırın:

```sh
sh "$Y" setup --agent codex --self-agent codex --default-agent codex
sh "$Y" setup --agent codex --self-agent codex --default-agent codex --apply
```

`research-node` üzerinde `grok` ajanını yapılandırın:

```sh
sh "$Y" setup --agent grok --self-agent grok --default-agent grok
sh "$Y" setup --agent grok --self-agent grok --default-agent grok --apply
```

İlk komut değişiklikleri önizler; ikincisi uygular. Erişim politikasının ve ajan adlarının amacınıza uyduğunu gözden geçirin. Kurulum mevcut girdileri korur ve değişen yapılandırmayı yedekler. Mevcut bir kimliği veya bağdaştırıcıyı değiştirmek açık bir değiştirme seçeneği gerektirir.

Yeni kurulumlar `tailnet` kipini kullanır: ağ politikanızın geçmesine izin verdiği kimliği doğrulanmış Tailscale eşleri, yerel bir `deny` kuralıyla eşleşmedikçe gönderebilir. Makine başına izin listesi gerekmez. Kısıtlı bir yeni kurulum için `--allow PEER` ekleyin; bu `allowlist` kipini seçer. `--send-only` boş bir izin listesiyle başlar. Mevcut kurulumlar eski izin listeleri dahil politikalarını korur.

Mevcut bir kurulumu yeni varsayılana geçirmek için yerelde inceleyip uygulayın:

```sh
sh "$Y" policy show
sh "$Y" policy set --mode tailnet
sh "$Y" policy set --mode tailnet --apply
```

Değiştirilen yapılandırma yedeklenir. Her iki kipte de `deny` kazanır; kurallar bir ana bilgisayar adı, FQDN, `tag:NAME` veya `user:LOGIN` olabilir. `--allow` veya `--deny` ayarlamak o listenin tamamını değiştirir; belirtilmeyen listeler değişmeden kalır. Red örnekleri ve çevrimdışı cihazlar için [0.4 geçişi](docs/UPGRADING-0.4.md) belgesine bakın. Mesaj erişimi uyandırma, görev yürütme veya otomatik yanıt izni vermez.

### 2. Her iki makinede alıcıyı çalışır durumda tutun

İlk test için **her makinede** ayrı bir terminalde şunu çalıştırın ve açık tutun:

```sh
sh "$Y" serve
```

Sürekli kullanım için kararlı bir çalışma zamanı kurun ve bir servis tanımı yazdırın:

```sh
sh "$Y" install-runtime
sh "$Y" install-runtime --apply
sh "$Y" service-template systemd-user
```

`systemd-user` yerine macOS'ta `launchd` veya Windows şablonu için `windows` kullanın. Kurmak ve başlatmak için üretilen tanımdaki talimatları izleyin. Bu komutlar servisleri kendileri başlatmaz. Aynı portta servisi başlatmadan önce ön plan alıcısını durdurun. Ardından başka bir terminalde `sh "$Y" doctor` çalıştırın.

### 3. Merhaba deyin, veri gönderin ve dosya teslim edin

`agent-laptop` üzerinden, kendi yetkili alıcınız ve dosyalarınızla:

```sh
sh "$Y" send research-node/grok "The brief is available." --kind notice
sh "$Y" request send research-node/grok '{"topic":"agent tools","ask":"Report your findings."}' --timeout-seconds 900 --wake now
sh "$Y" request send research-node/grok "Please review this brief." --file ./brief.md --timeout-seconds 900
```

Önce `brief.md` oluşturun veya paylaşmak istediğiniz mevcut bir dosyayı seçin. JSON çalıştırılabilir talimat değil, mesaj içeriğidir. Mesaj zarfı varsayılan olarak 256 KiB ile sınırlıdır; daha büyük veriler için ek kullanın. Birden fazla dosya eklemek için `--file` parametresini tekrarlayın.

### 4. Okuyun ve yanıtlayın

`research-node` üzerinde `MESSAGE_ID` değerini `inbox` tarafından gösterilen kimlikle değiştirin:

```sh
sh "$Y" inbox --pending --agent grok
sh "$Y" show MESSAGE_ID
sh "$Y" fetch MESSAGE_ID
sh "$Y" request respond MESSAGE_ID "Started reviewing." --state running
sh "$Y" request respond MESSAGE_ID "Reviewed. Here is my summary." --state completed
sh "$Y" ack MESSAGE_ID
```

`fetch` ekleri indirir ve doğrular. Takipli bir istek için yetkili çalışmadan önce `running` ve `ack` öncesinde son bir `completed`, `failed`, `rejected` veya `blocked` sonucu gönderin. `reply ID TEXT` de takipli istekler için son bir `completed` yanıtı kaydeder; ilerleme için `request respond --state running` kullanın. Sonuç dosyalarını `request respond ID TEXT --state completed --file PATH` veya `reply ID TEXT --file PATH` ile ekleyin; bunlar kimliği doğrulanmış özgün göndericiye hitap eder. `ack` mesajı **yalnızca bu makinede** işlendi olarak işaretler ve takipli istekler için kalıcı bir son yanıt gerektirir. Uyandırma bağdaştırıcısı olmadan ajanınız gelen kutusunu siz istediğinizde veya kendi zamanlama mekanizması çalıştığında kontrol eder.

## Alındı bilgili ve son sonuçlu istekler

0.5 sürümünde `request send` ve `send --kind request` varsayılan olarak takipli bir istek oluşturur. Eş `/info` içinde `request-lifecycle-v1` duyurmalıdır; daha eski eşler yükseltme gerektirir. Yalnızca takipli alındı veya sonuç garantisi olmadan sıradan bir isteği açıkça kabul ettiğinizde `send --kind request --legacy-request` kullanın. Mevcut v1 mesajları uyumlu kalır.

Alıcı, kalıcı depolama sonrasında otomatik olarak minimal bir `received` alındısı kaydeder. Ajan ilerlemeyi ve son sonucu bildirir; izin veya ortam engelleri başarı değil `blocked` olarak raporlanmalıdır. Bildirimler (`--kind notice`, yükseltme bildirimleri dahil) yanıt gerektirmez. Alındılar ve yanıtlar `wake: none` kullanır ve yeni yanıtlar üretmez.

Sessizliğin başarı anlamına gelip gelmediğini tahmin etmeden bir isteği kontrol edin:

```sh
sh "$Y" request status REQUEST_ID --json
sh "$Y" request status REQUEST_ID --refresh --json
sh "$Y" request list --json
```

`--refresh` olmadan durum yerel defteri okur. Bununla birlikte komut kimliği doğrulanmış özgün alıcıyla uzlaşma yapar. `received` saklandı; `running` ajanın başladığını bildirdiği anlamına gelir. `completed`, `failed`, `rejected` ve `blocked` son sonuçlardır. Son teslim süresine kadar (varsayılan 900 saniye) son sonuç gelmezse `timed_out`, **gerçek yürütme sonucunun bilinmediği** anlamına gelir. İşi iptal etmez veya tekrarı güvenli kılmaz. Geciken bir son yanıt sonucu günceller ve `late` olarak işaretlenir; başka bir istek göndermeden önce uzlaştırın.

Takipli yanıtlar sıradan gelen kutusunun dışında, kimliği doğrulanmış istek defterinde kalır ve ajanı asla uyandırmaz. Bir sonucu incelemek veya eklerini indirmek için `request status REQUEST_ID --json` çıktısından `response.id` alın, ardından `show RESPONSE_ID` veya `fetch RESPONSE_ID` çalıştırın. Çekilen dosyalar boyut ve SHA-256 doğrulamasından sonra `<home>/inbox/RESPONSE_ID/files/` altında görünür. Bir protokol yanıtını yeni bir görev gibi yanıtlamayın veya onaylamayın.

Alındılar ve sonuçlar çalışan alıcı tarafından kararlı yanıt kimlikleriyle kalıcı olarak kuyruklanır ve yeniden denenir; `request maintain` elle bir bakım geçişi çalıştırır, gelen kutusu kurtarma, süre sonu ve teslimatı bağımsız olarak dener ve alıcı zaten teslimat yapıyorken `busy` bildirir. Alıcı bu bakım işçilerini denetler: duran bir işçi yenilenir, yavaş biri asla çoğaltılmaz ve çevrimdışı bir eş süre sonunu veya başka bir eşin yanıtlarını bekletemez. Bakım aksadığında `/health` yine yanıt verir ancak `status: degraded` ile `ok: false` raporlar ve `doctor` bakım hazırlığını erişilebilirlikten ayrı gösterir. Otomatik uzlaştırma için her iki taraf da verilerini saklamalı ve alıcılarını çalıştırmalıdır. Çevrimdışı bir makine veya hiç çalışmayan bir ajan bitirmeye zorlanamaz. Mekanizma ajanın bildirdiğini izler; görev başarısı önemli olduğunda kanıtları doğrulayın.

Codex iş yanıtları hâlâ yerel yetkilendirme gerektirir. `allow_replies=false` köprünün modeli çalıştırmadan minimal bir `blocked` sonucu kaydetmesini sağlar; yanıtları sessizce etkinleştirmez. `--allow-replies` seçeneğini açıkça açmak, eşlerden gelen keyfî talimatların yürütülmesine değil, sahibin mevcut kapsamı içindeki yanıtlara izin verir. Özelleştirilmiş dağıtıcı talimatları yükseltme sırasında yeni "ack öncesi sonuç" iş akışını benimsemelidir.

### Telefonunuzda imzalanan onayınız (0.7.30)

Bir iş onayınızı gerektirdiğinde ajan bir ifade yazmanızı istemek yerine size bir bağlantı verir. `yobiru.com/approve` üzerinde tam olarak neyi onayladığınızı okur ve Face ID, parmak izi veya cihaz PIN'inizle doğrularsınız; bu işlem cihazınızdan asla çıkmayan bir geçiş anahtarıyla (passkey) imzalanır. İşi yapan bilgisayar, ajanı isteği görmeden önce imzayı kendisi kontrol eder: ajanına bir kez "Set up my Yobiru owner passkey" deyin, ardından gönderen ajana "Ask my approval for claude on mac-studio to deploy the site, then send it." deyin. 0.7.31'den itibaren aynısı Friends üzerindeki kendi botlarınız arasında da çalışır: diğer botunuzdan botunuza yazdığınızda, onun ajanı bağlantıyı oraya geri gönderir ve yalnızca o tek mesajı imzaladıktan sonra harekete geçer. 0.7.32'den itibaren Claude Code da otomatik modu bir şeyi durdurduğunda aynı şekilde çalışır: Yobiru koruması kuruluyken (Claude Code'a "Install the Yobiru guard" deyin), onayınızı doğru oturuma yazmanızı istemek yerine size tam o çağrı için bir bağlantı gönderir. Bkz. [Sahip onayları](docs/APPROVALS.md).

## Uyandırmalar ve teslimat durumu

Göndericiler `--wake now`, `batch` veya `none` isteyebilir; son kararı alıcı verir. `yobiru-wake` becerisiyle yerel bir çalıştırılabilir dosya veya webhook yapılandırın. `wake-config` `--apply` eklemedikçe değişiklikleri önizler ve gönderici/ajan sınırlarını, sessiz saatleri ve hız sınırlarını destekler. Webhook URL'leri ve kimlik bilgileri özel dosyalar veya ortam değişkenleri aracılığıyla referans verilir.

Grok Bot için hesap tarafından desteklendiğinde bir rutin webhook'u bağdaştırıcı hedefi olabilir. Bkz. [rutin webhook'ları](https://cursor.com/help/grok-bot/routines).

### İsteğe bağlı Codex köprüsü (macOS masaüstü veya Linux app-server)

`codex-bridge`, kimliği doğrulanmış mesajları dağıtıcı olarak görev yapan **mevcut tek bir Codex oturumuna** kuyruklar: macOS'ta Codex masaüstü uygulaması üzerinden, Linux'ta kullanıcı systemd servisi ve kendi çalıştırdığınız Codex app-server üzerinden. İkisi de resmî `codex queue` komutunu kullanır ve tekilleştirme ve yeniden denemelerle tek bir kalıcı SQLite kuyruğunu paylaşır. Eklenti Codex'i başlatmaz. Linux'ta Codex CLI, yerel bir Unix soketinde aynı kullanıcı olarak çalıştırdığınız bir app-server, oturumun UUID'si, çalışan bir `systemctl --user` ve `install-runtime` ile kurulmuş bir alıcı gerekir:

```sh
sh "$Y" codex-bridge install --thread-id SESSION_UUID --codex-remote "unix:///absolute/path/to/app-server.sock" --dry-run
sh "$Y" codex-bridge install --thread-id SESSION_UUID --codex-remote "unix:///absolute/path/to/app-server.sock"
sh "$Y" codex-bridge status
```

`--dry-run` hiçbir şey yazmaz ve hiçbir servis yöneticisini çağırmaz; `--no-start` yalnızca dosyaları yazar. Yalnızca aynı kullanıcılı yerel soket kullanılır (ağ uç noktası yok, yeni oturum yok, `--last` yok). Köprü `codex_home` değerini sabitler (`--codex-home`, aksi hâlde kurulu değer, ilk kurulumda `$CODEX_HOME`, yoksa `~/.codex`), böylece uzlaştırma app-server ile aynı hesabı okur. Otomatik yanıtlar yeni kurulumlarda kapalıdır ve onayınızla `--allow-replies` gerektirir; yükseltmeler mevcut yetkili ayarı korur (`--no-allow-replies` kapatır). `enqueued`, CLI'ın bildirimi kabul ettiği anlamına gelir (kuyruk kimliği saklanır), görevin başladığı veya bittiği anlamına gelmez; `delivered` yalnızca bildirim işaretçisinin oturumda göründüğü anlamına gelir. `uncertain` bir kuyruk denemesinin Codex'e ulaşmış olabileceği anlamına gelir: asla otomatik yeniden gönderilmez ve `status` nasıl kontrol edileceğini açıklar. `status` ayrıca salt-okunur `diagnostics` gösterir: hangi kanıtların mevcut olduğu, başlama kanıtı olmadan kabul edilen iş veya son sonuç olmadan geçen son teslim süresi gibi `execution_unknown` nedenleri ve aktarımın neleri gözlemleyemediği. Masaüstü aktarımının kalıcı bağlantısı yoktur; bu nedenle kapalı, hız sınırlı veya bellekten düşmüş bir oturum başlama kanıtı olmadan kabul edilen iş gibi görünebilir; bu belirti tek başına hangisinin neden olduğunu kanıtlamaz. Çalışma zamanını yükseltmek kurulu bir köprüyü güncellemez: aynı UUID ile yeniden kurun. Ayrıntılar, izinler ve geri alma: [Codex köprüsü kurulumu](CODEX_LOCAL.md) (Çince).

Mevcut kurulumlar seçili mesaj türlerini dağıtıcı oturumunun dışında tutabilir: `codex-bridge.json` içinde `excluded_kinds` ve `excluded_kind_adapter` (güvenilir bir yerel programın mutlak argv'si). Bu mesajlar Codex kuyruğuna asla girmez. Yalnızca etkin düzeyi `now` olan doğrulanmış, onaylanmamış bir mesaj için doğrudan alıcı uyandırması bu programı, tek ek argüman olarak gelen kutusu JSON yoluyla (kabuk yok, mesaj metni yok) çalıştırır ve `excluded_kind_timeout_seconds` (varsayılan 60) sonunda durdurur. Taramalar, yeniden denemeler ve durum kontrolleri bunu asla çalıştırmaz. Etkin sınır ayrıca alıcının kendi `hook_timeout_s` süresinden 10 saniye önce biter, böylece işleyici ve süreç grubu önce durdurulur. Program başladıktan sonra başarısız olsa veya zaman aşımına uğrasa bile köprü 0 ile çıkar (sonuç yazdırılır ve günlüğe kaydedilir), böylece asla otomatik tekrar oynatılmaz; mesaj gelen kutusunda kalır ve program mesaj kimliğine göre tüm yeniden deneme ve tekilleştirmeden sorumlu olur. Mevcut yerel dağıtıcı talimatları yükseltmede bayt bayt korunur; şablon değişikliklerini kendiniz birleştirin. Yükleyiciler bu alanları `--dry-run` içinde doğrular ve ince ayarlı zamanlamalar, özel dağıtıcı talimatları, kayıtlı Codex CLI ve yetkili yanıtlarla birlikte yükseltmede korur.

**Kalıcı oturum iş akışı.** Sabit dağıtıcıyı kısa bildirimler ve protokol işleme için hazır tutun. Aynı konu için bir iş oturumunu yeniden kullanın ve yeni işler için ayrı bir oturum oluşturun; yürütme ve insan bekleme orada yapılmalıdır ki yavaş tek bir görev diğer mesajları bekletmesin. Devri kalıcı hâle getirin, paylaşılan kaynakları kararlı bir `resource_key` anahtarlı gerçek kilitler veya kiralamalarla koruyun ve sonuçları doğrulayıp isteği tamamlamak için tek bir `lifecycle_owner` atayın. Bu kurallar yinelenen işi ve kayıp sonuçları azaltır. Ana ajana rehberlik ederler: köprü bildirimleri kalıcı olarak kuyruklar, ancak iş oturumlarının çalışmaya devam edeceğini veya otomatik kurtarılacağını garanti edemez.

[Dağıtıcı şablonunu](CODEX_DISPATCHER.md) ve [gelen kutusu](plugins/yobiru/skills/yobiru-inbox/SKILL.md)/[uyandırma](plugins/yobiru/skills/yobiru-wake/SKILL.md) becerilerini Git'te sürdürün, ardından `python3 tools/build_plugin.py sync` ile dahili şablonu eşitleyin. Kurulu eklenti önbellekleri doğruluk kaynağı değildir; mevcut özelleştirilmiş dağıtıcı talimatları yükseltirken bilinçli bir birleştirme gerektirir. Devir yaşam döngüsü ve doğrulama adımları için [oturum bakımı ve gerekçesi](docs/DISPATCHER_SESSIONS.md) belgesine bakın.

| Sinyal | Neyi kanıtlar? |
|---|---|
| `send` komutundan HTTP `202` | Alıcı mesajı kabul etti ve sakladı. |
| Başarılı bir uyandırma bağdaştırıcısı | Bildirim veya webhook kabul edildi; ajan henüz başlamamış veya hâlâ çalışıyor olabilir. |
| `ack` | Alıcı mesajı yerelde işlendi olarak işaretledi; göndericiye bildirilmez. |
| Sonucu açıklayan bir yanıt | Ajan kendi sonucunu bildirir. Tamamlanma önemli olduğunda sağlanan kanıtları kontrol edin. |

Yobiru tam olarak bir kez görev yürütme veya sıradan mesajların otomatik yeniden teslimatını sağlamaz. Takipli istek yanıtları kalıcı bir yeniden deneme giden kutusu kullanır; uyandırma denemelerinin sınırlı yeniden deneme hakkı vardır. Mesaj kimliklerini, yerel onayı, bildirilen sonuçları ve doğrulanmış görev başarısını ayrı kavramlar olarak ele alın.

## Hangi ajanlar çevrimiçi ve hangilerini hangi oturum yönetiyor?

```sh
sh "$Y" agents --online          # `peers` aynı komuttur; çevrimdışı makineleri de listelemek için --online kaldırın
```

Her makine için: Tailscale çevrimiçi durumu, alıcısının yanıt verip vermediği, bu makinenin oraya gönderip gönderemeyeceği, Yobiru sürümü (protokol sürümü değil) ve ajan başına **kalıcı oturum**: kaynağıyla birlikte yerel adı (`native codex session name, read <time>`, önbellekli/eski), kullanıcı onaylı herhangi bir başlık veya yerel takma ad (böyle etiketlenmiş olarak) ve her zaman tam oturum kimliği. Meşgul/boşta durumu asla iddia edilmez. Alıcılar `GET /info` isteğine yalnızca erişim politikalarının izin verdiği kimliği doğrulanmış makineler için yanıt verir (403 ağ hatası değil politikadır) ve hiçbir yol, bağdaştırıcı ayarı, belirteç, mesaj önizlemesi veya oturum listesi döndürmez. Eski alıcılar 404 döndürür ve çevrimdışı değil `legacy` (sürüm ve oturumlar bilinmiyor) olarak gösterilir.

Bir ajanı yöneten oturumu tam kimliğiyle adlandırın; yalnızca bir ad asla bir oturumu seçmez veya yeniden adlandırmaz:

```sh
sh "$Y" session bind --agent grok --runtime grok --session-id CHAT_ID --display-name "Research desk"
sh "$Y" session bind --agent grok --runtime grok --session-id CHAT_ID --display-name "Research desk" --apply
```

Codex köprüsü kuruluysa sabit dağıtıcı UUID'si otomatik bildirilir ve yerel adı (`Thread.name`, yalnızca meta veri okuyan `thread/read` ile okunur) bir yeniden adlandırmadan yaklaşık beş dakika içinde yenilenir; köprüyle çakışan bir bağlama reddedilir. Bağlama bir oturumu adlandırır; uyandırma ayrı yapılandırılır (köprü veya `wake-config`).

## Gizlilik ve güven

Depolama, ağ bağlantıları ve silme ayrıntıları için [gizlilik bildirimine](docs/PRIVACY.md) bakın.

- **Verileri makineleriniz tutar.** Gelen/giden kutusu kayıtları ve hazırlanan ekler veri dizininde yaşar: `--home DIR`, ardından `$TAILMSG_HOME`, ardından `~/.tailmsg`. Özel mesajlaşma merkezi depolama servisi kullanmaz. İsteğe bağlı herkese açık bot ağ geçidini çalıştırırsanız, o botun herkese açık sohbetleri için sizin işlettiğiniz merkezi bir depo olur. Seçilen ajan sağlayıcıları kendi ajanlarına verilen içeriği işlemeye devam edebilir.
- **Tailscale ağ sınırıdır.** Alıcı Tailscale kimliğini ve kendi erişim politikasını kontrol eder (yeni kurulumlar için varsayılan `tailnet`, isteğe bağlı `allowlist`, red kuralları önceliklidir). Tailscale ACL'leri/izinleri geçerliliğini korur. Aktarım sırasında şifreleme Tailscale'den gelir; Yobiru durgun veri şifrelemesi eklemez. Ana bilgisayarı ve dosya sistemini koruyun.
- **Ajan adları yalıtım değil yönlendirmedir.** `from_agent` gönderici tarafından seçilir. Bir makineyi veya veri dizinini paylaşan birden fazla ajan ayrı güvenlik ilkeleri değildir; gerektiğinde ayrı makineler veya uygun ana bilgisayar yalıtımı kullanın.
- **Mesajlar izin değil istektir.** Alınan metin ve dosyalar komutlara, satın alımlara, yapılandırma değişikliklerine veya ilgisiz verilerin ifşasına yetki vermez. Ajanlar kullanıcının mevcut talimatları ve izinleri dahilinde hareket eder.
- **Yalnızca geçiş anahtarı (passkey) imzanız onayınızdır.** Kabul ettiğinizi söyleyen bir mesaj onay değildir. Bir [sahip onayı](docs/APPROVALS.md) yalnızca alıcı bilgisayar imzanızı kendisi doğruladığında ve yalnızca imzaladığınız tam talimat için, ajanın mevcut izinleri dahilinde geçerlidir.
- **Ekler dikkatli ele alınmalıdır.** Boyut ve SHA-256 ile doğrulanırlar, ancak bu içeriklerinin çalıştırılması güvenli olduğu anlamına gelmez. Süresi dolan bağlantılar önceden indirilmiş kopyaları iptal etmez.
- **Yalnızca amaçlanan verileri paylaşın.** Mesajlara kimlik bilgisi veya ilgisiz özel dosyalar koymayın. Webhook gizli anahtarlarını korumalı dosyalarda veya ortam değişkenlerinde tutun; asla commit'lere veya ekran görüntülerine koymayın.

Bu proje resmî bir bağımsız güvenlik denetiminden geçmemiştir. Güvenilir ancak ele geçirilmiş bir eş, ele geçirilmiş bir ana bilgisayar veya aşırı yetkili bir ajan erişimi yine de kötüye kullanabilir.

## Güncelleme ve kaldırma

Codex için:

```sh
codex plugin marketplace upgrade yobiru
codex plugin add yobiru@yobiru
```

Claude Code için: `claude plugin marketplace update yobiru`, ardından `claude plugin update yobiru@yobiru`.

Güncellemeden sonra yeni bir sohbet başlatın. Cursor'da **Customize** bölümünden güncelleyin veya yerel eklenti kopyasını değiştirin; Grok Bot'ta **Connect apps** üzerinden yönetin.

Bir eklenti güncellemesi kalıcı alıcıyı **güncellemez**: `install-runtime --upgrade` (ardından `--apply`) çalıştırın ve alıcı servisini yeniden başlatın; köprü kuruluysa aynı oturum UUID'siyle `codex-bridge install` komutunu yeniden çalıştırın (adlar, yanıt ayarı ve özel dağıtıcı talimatları korunur). Ardından diğer makinelerinizi koordine edin:

```sh
sh "$Y" update check                     # yalnızca resmî sürümler; --channel preview ön sürümleri de içerir
sh "$Y" update status                    # kurulu eklenti, kalıcı çalışma zamanı, çalışan alıcı, bildirimler
sh "$Y" update notify --channel preview  # deneme çalıştırması; inceledikten sonra --apply ekleyin
```

`update check` hiçbir şey kurmaz ve varsayılan kararlı kanalı 0.7.0-beta.1 gibi bir ön sürümü asla önermez; `--channel preview` bunu tam etiketine, arşivine ve sağlama toplamı dosyasına sabitlenmiş adımlarla listeler. Ön sürüm duyurmak `--channel preview` gerektirir; yeniden deneme zamanlayıcısı da öyledir (`update reminder-template ... --retry-notices --channel preview`): kararlı bir zamanlayıcı ön sürüm bildirimlerini tutar. `install-runtime --upgrade`, `--allow-downgrade` eklemedikçe geliştirme derlemesi gibi eklentiden daha yeni bir çalışma zamanını korur.

`notify`, yalnızca bu makinenin eklentisi, çalışma zamanı ve çalışan alıcısı yayımlanmış bir resmî sürümü çalıştırdığında ve yalnızca bilinen kurulumlara (keşfedilen alıcılar, `--peer`, `agents --remember` ile hatırlanan kurulumlar, önceki bekleyen bildirimler) sürümlü bildirim gönderir. Her makine ve ajanın kalıcı bir durumu vardır: `notified` (mesaj kimliği için alındılı 202; yükseltme kanıtı değildir), `verified` (sonraki keşif o sürümün çalıştığını gösterir; yalnızca yazılım), `pending` (çevrimdışı, sonraki çalıştırmada yeniden denenir), `blocked` (403; yalnızca o alıcının `/info` yanıtı erişim gösterdiğinde veya `--retry-blocked` ile yeniden sorulur), `rejected` veya `uncertain` (asla otomatik yeniden gönderilmez; `--reconcile-uncertain` aynı mesaj kimliğini yeniden gönderir). Bildirimler `wake: now` ister; yine de her alıcının uyandırma politikası karar verir. Alınan bildirim ajan ve sahibi için bir bilgidir, komut değildir. Bildirimler yanıt gerektirmez; bir yükseltme sonucu gerektiğinde ayrı bir yetkili takipli istek gönderin. İsteğe bağlı hatırlatıcılar: `update reminder-template systemd-user|launchd|cron` haftalık bir `update check --record` işi yazdırır.

Çevrimdışı kurulumlar için önce `update notify --peer HOST/AGENT --apply` ile bir bildirimi yetkilendirin; o alıcı erişilebilir olana kadar bekler. `update maintain --apply` bir güncelleme kontrolü kaydeder ve **yalnızca önceden kuyruklanmış yükseltme bildirimlerini** yeniden dener veya doğrular. Başarılı resmî sürüm meta verileri altı saat önbelleklenir. Yeni alıcı kaydetmez, yazılım kurmaz veya uzak politikayı değiştirmez. Her 15 dakikada bir ve başlangıçtan sonra çalıştırmak için isteğe bağlı zamanlayıcıyı üretip kurun:

```sh
sh "$Y" update maintain                         # önizleme; hiçbir şey yazılmaz veya gönderilmez
sh "$Y" update reminder-template systemd-user --retry-notices
# uygun yerlerde systemd-user yerine launchd veya cron kullanın
```

Şablon talimatları yazdırır; kendini kurmaz. Betik yolu değişebileceğinden eklenti güncellemelerinden sonra yeniden üretin. `uncertain` bildirimler hâlâ açık uzlaştırma gerektirir. Kapalı bir cihaz veya durmuş bir alıcı dönene kadar alamaz veya yükseltemez; politikayla engellenmiş eski bir kurulum sahibinin yerel geçişini gerektirir. Bu kurtarma sıradan mesajlar için değil yükseltme bildirimleri için geçerlidir. Bkz. [0.4 geçişi ve doğrulama](docs/UPGRADING-0.4.md).

Codex eklentisini ve pazar yerini kaldırmak için:

```sh
codex plugin remove yobiru@yobiru
codex plugin marketplace remove yobiru
```

Claude Code için: `claude plugin uninstall yobiru@yobiru` ve `claude plugin marketplace remove yobiru`.

Eklentiyi kaldırmak verilerinizi, alıcı servisini ve varsa Codex köprüsünü dokunmadan bırakır. Tamamen kaldırmak için önce servisleri durdurup kaldırın, ardından mesajlarına ve dosyalarına artık ihtiyacınız olmadığında veri dizinini silin.

Mevcut tailmsg kurulumları uyumlu kalır: `tailmsg.py`, protokol v1, `~/.tailmsg`, `TAILMSG_*` ve mevcut servis adları korunur.

## Geliştirme ve doğrulama

```sh
python3 tools/build_plugin.py sync
python3 tools/build_plugin.py check
python3 -m unittest discover -s tests -v
python3 -m unittest -v test_codex_bridge test_codex_queue_client test_install_codex_bridge test_install_codex_bridge_linux
python3 tools/build_plugin.py release
```

`sync` bildirimleri ve bayt düzeyinde özdeş dahili kaynakları yeniden üretir. `check` sapmaları ve paketleme sorunlarını yakalar. `release`, `dist/` içinde tekrarlanabilir eklenti ve pazar yeri ZIP'leri ile `SHA256SUMS` oluşturur. Bkz. [sürüm talimatları](docs/RELEASING.md) ve [aktarım protokolü](SPEC.md).

| Alan | Mevcut doğrulama sınırı |
|---|---|
| Paketleme ve CLI | Otomatik kontroller çıkarılmış arşivleri, boşluklu yolları, yapılandırma korumasını ve yalıtılmış düzeneklerle mesaj/dosya/yanıt/uyandırma akışlarını kapsar. |
| Tailscale aktarımı | Tarihsel 0.4.0 tek ana bilgisayarlı öz testleri gerçek bir Tailscale arayüzünde 59 kontrolü geçti. Ayrı 0.5.0 canlı kabulü gerçek bir makineler arası Grok Bot isteğini içerir; tarihli doğrulama notlarına bakın. |
| Codex | Tarihsel depo kurulumu ve beceri keşfi geçti. 2026-10-13'te mevcut oturumundaki canlı bir 0.5.0 Linux köprüsü otomatik takipli bir yanıtı tamamladı. Bu her masaüstü ana bilgisayarını veya herkese açık dizin kurulum rotasını kanıtlamaz. |
| Claude Code | Otomatik: `claude plugin validate --strict` eklenti ve pazar yeri için geçer; Claude Code 2.1.306 eklentiyi yerel pazar yerinden kurdu ve MCP sunucusuna bağlandı; loopback testleri iki yalıtılmış dizin arasında o sunucuyu (alma, okuma, çekme, yanıtlama, takipli istek, ack) çalıştırır. 2026-10-21'de canlı: bir Mac Studio (Claude Code 2.1.306 eklentiyi yükledi ve 10 aracını listeledi) eklentinin MCP sunucusu üzerinden bir Grok Bot bulut bilgisayarıyla (0.6.0-beta.1) mesajlaştı: takipli bir istek yaklaşık 26 saniyede tamamlandı ve Grok Bot'un mesajı okundu, yanıtlandı ve onaylandı. |
| Cursor / Grok Bot / DOTS | Elle konuşlandırılmış bir Grok Bot 0.5.0 çalışma zamanı gerçek bir makineler arası takipli isteği ve kimliği doğrulanmış durum yenilemesini tamamladı. 2026-11-12'de resmî Tailscale eklentisi (bağlayıcı rotası) üzerinden bağlı 0.7.27 sürümündeki bir Grok Bot, 0.7.26 alıcısıyla dosyalarla gidiş-dönüş tamamladı. Yerel Grok/Cursor pazar yeri kurulumu ve yeniden kurulum kurtarması ayrı kabul adımları olarak kalır. DOTS bağlayıcısı sunulmaz. |
| 0.3.0 katılım, keşif, güncellemeler | Çevrimdışı testler: platform bazlı katılım (eksik, oturum kapalı, daemon, izin, TUN yok), kimliği doğrulanmış `/info` ve gizli bilgisiz yansıtma, UUID ile yerel/onaylı/takma adlar, eski 404 / 403 / erişilemez / bozuk / yavaş eşler, sürüm ayrıştırma ve kanallar, bildirim tekilleştirme, yeniden denemeler ve belirsiz durum işleme, adları koruyan köprü yükseltmeleri. Gerçek Tailscale, GitHub veya eşle iletişim kurulmadı. |
| Herkese açık pazar yeri listelemeleri | Bkz. [başvuru kaydı](docs/MARKETPLACE_SUBMISSION.md). Depo kurulumu, başvuru, inceleme ve yayımlama ayrıdır. |
| Friends | Otomatik testler gerçek bir ağ geçidini, bağlayıcıyı, deterministik işçiyi, takvim ve kaynak servislerini ve çağırıcıları sentetik verilerle loopback üzerinde ayrı yerel süreçler olarak çalıştırır; sentetik bir ana süreç sahibin ajanını canlandırır. Şema-3 eklemeleri aynı şekilde kapsanır ve resmî MCP SDK istemcisinin geliştirici çalıştırması yerelde geçti; hiçbir barındırılan MCP platformu test edilmedi. Gerçek bir yerel Codex ajanı önceki bir anlık görüntü üzerinden gözetimli iki turlu bir sohbet tamamladı. 2026-10-22'de yerel bir Grok Bot gerçek bir ağ geçidine karşı on iki kabul kontrolünün tamamını geçti; gerçek takvim veya ana bilgisayar yalıtımı doğrulanmadı. Bkz. [herkese açık botlar ve Friends](docs/PUBLIC_BOTS.md#what-has-and-has-not-been-verified). |

`sh selftest.sh` Tailscale ve geçici boş `7866–7883` portlarını gerektirir; üretim portu `7766`'yı veya varsayılan veri dizinini kullanmaz. Kalan kabul adımları için [platform notları ve kaynaklarına](docs/PLATFORMS.md) bakın.

Ayrıntılı sürüm kontrolleri [doğrulama notlarında](docs/VERIFICATION.md) kayıtlıdır.

## Katkıda bulunma ve lisans

[chekusu/yobiru](https://github.com/chekusu/yobiru) adresinde issue ve pull request'lerinizi bekliyoruz. İşletim sisteminizi, ana bilgisayar sürümünü, minimal yeniden oluşturma adımlarını ve temizlenmiş çıktıyı ekleyin. Tanılama paylaşmadan önce IP adreslerini, tailnet adlarını, hesap tanımlayıcılarını, mesaj içeriklerini ve kimlik bilgilerini kaldırın. Güvenlik açığı şüphesi için varsa GitHub'ın özel **Report a vulnerability** seçeneğini kullanın; herkese açık bir issue içinde gizli bilgi paylaşmayın.

`README.md` dosyasını kanonik İngilizce belge olarak koruyun ve Çince, Japonca ile Türkçe sürümleri birlikte güncelleyin. Kod değişiklikleri göndermeden önce yukarıdaki kontrolleri çalıştırın. Aktarım protokolündeki değişiklikler mevcut eşlerle birlikte çalışabilirliği korumalıdır.

Yobiru **Chekusu** altında sürdürülmekte ve [MIT Lisansı](LICENSE) altında yayımlanmaktadır. `tailmsg` projesinden doğmuştur; CLI ve protokol bu uyumluluğu korur. Grok Bot, DOTS, Codex, Cursor ve Tailscale ilgili ürünlerinin adlarıdır; burada geçmeleri onay anlamına gelmez.

[Son logo seti](brand/logo/yobiru/README.md) sembol, yazı tipi logosu ve birleşik logoyu SVG ve PNG olarak içerir. Web sitesi renkli varlıkları yansıtır; iki kopyadan birini güncellemeden önce [`docs/RELEASING.md`](docs/RELEASING.md) dosyasına bakın.

## Geliştirme

Sürekli geliştirme Sandbank Tokyo üzerinde yürütülür; Mac kopyası eşitleme ve macOS doğrulaması için kullanılır. Worktree, kontrol noktası ve çalışma zamanı sınırları için [geliştirme ve devir](docs/DEVELOPMENT.md) belgesine bakın.
