# Независимое ревью безопасности FoxTube — 2026-11-11

Подтверждённых открытых дефектов P0–P3 в проверенном коде не обнаружено. Это результат анализа перечисленных границ и проверок, а не гарантия отсутствия уязвимостей.

Проверена текущая ветка с незакоммиченными изменениями, включая новые файлы canonical origin, persistent mini, recommendations/autoplay и изменения LAN/MSS. Исходный HEAD: `89ec6c13fe87e05a871d891f02bba182b6485468`. Хеши исследуемого снимка: `.artifacts/audit-2026-11-11/security-source-snapshot.json`. Код приложения, рабочие данные и конфигурация хоста ревьюером не изменялись. Записаны только этот отчёт и назначенные security-артефакты.

Прочитаны AGENTS.md, TestTask.md, архитектура, безопасность, текущее состояние и навык `.agents/skills/foxtube-security/SKILL.md`. Рассмотрены анонимный клиент, обычный пользователь, получатель grant, владелец, moderator/admin и загрузивший недоверенный файл пользователь. Проверены HTTP/API → сессия → сервис → SQL → файловая система/Nginx/worker, а также новые клиентские переходы.

## Результат по поверхностям

| Поверхность | Что проверено | Результат |
| --- | --- | --- |
| Auth/session/CSRF/canonical origin | `auth/{routes,dependencies,service,schemas,passwords,rate_limits}.py`, `core/{canonical_origin,request_limits,security_headers,config}.py`, порядок middleware в `main.py` | Случайные непрозрачные cookie; в БД хеш и отзыв; ротация с блокировкой и повторной проверкой сессии; свежая роль из БД; точный Origin и constant-time CSRF; лимиты IP/email и Argon2; ограничение тела независимо от Content-Length. GET/HEAD HTML-redirect на фиксированный origin после TrustedHost не переносит мутации и не использует входной Host/query как адрес назначения. |
| Объектные права/списки | `videos/service.py`, `channels/service.py`, `social/{service,routes,web}.py`, SQL constraints/migrations | Общий readable: READY, visible, не удалено и public/владелец/grant. Роли moderator/admin не входят в обход private ACL. Права применены до limit/count в видео, поиске, каналах, ленте, содержимом и счётчиках плейлистов. Владелец отдельно получает состояние своего неготового/скрытого видео с пустым playback URL. |
| Медиа/HEAD/Range | `videos/routes.py:media`, `videos/storage.py`, `deploy/nginx/nginx.conf` | GET и HEAD проходят один ACL; ключи файлов не выдаются клиенту. X-Accel-Redirect ведёт в `internal` location, прямой путь закрыт; symlink запрещён; Range обслуживается после проверки API; private/no-store. Новые запросы после revoke получают текущий ACL. Уже начатая передача и ранее полученные байты не считаются отзываемыми. |
| Social/moderation | Сервисы и маршруты social/moderation, функции миграции `0005_moderation.py`, DB grants | Мутации требуют CSRF и пользователя; владельцы объектов проверяются сервисами. Мутации, зависящие от video ACL, повторно проверяют его после ожидания блокировки. Staff-карточки маскируют недоступные title/watch URL. SECURITY DEFINER использует фиксированный search_path, параметры, проверку роли в БД и аудит; глобальная блокировка защищает последнего администратора. Публичного bootstrap admin нет. |
| Загрузка/квоты | `videos/{routes,service,upload,limits,storage}.py` | Права, Content-Type, ожидаемый размер и общий резерв проверяются до чтения тела. Потоковая запись с фактическим ограничением байтов, lease/deadline, O_EXCL/O_NOFOLLOW, immutable UUID-именами, fsync и атомарной публикацией; соединение/транзакция не удерживаются на время тела. Квоты и конкурентные ограничения сериализуются в PostgreSQL. |
| FFmpeg/worker/cleanup | `videos/{transcode,processing}.py`, `worker.py`, constraints и grants worker | Subprocess без shell и секретов окружения, с ограничением времени/вывода/потоков и остановкой process group. Разрешены file + MOV/MP4/Matroska/WebM и ограниченные кодеки; проверяются размеры/длительность/потоки и готовый результат. READY требует актуального токена/lease и проверенного опубликованного результата; worker не меняет visibility/moderation. Cleanup сохраняет учёт при ошибках и не удаляет результат с неизвестным исходом commit. |
| Клиент/mini/autoplay | `app.js`, `auth.js`, `player.js`, `previews.js`, `miniplayer.js`, `recommendations.js`, lifecycle video/social/moderation/loading и шаблоны | Данные пользователя выводятся через autoescape/textContent; новых eval/inline-скриптов нет. HTML мягкой навигации берётся только из same-origin разрешённых server routes, проверяются redirect URL, тип, shell и viewer; scripts удаляются перед вставкой, meta CSRF обновляется. Текущий player повторно проверяет session и playback ACL. Авто-next проверяет session, source и target, защищён от поздних ответов и смены состояния. Blob WebVTT ограничен размером, токенизирован и освобождается; localStorage содержит только пользовательские настройки. |
| Поставка/порты/секреты | Compose/Dockerfiles, Nginx, PostgreSQL init/grants, bootstrap/configure/configure_lan, demo, ignore-файлы | Опубликован только Nginx; API/БД без published ports и host mode. Непривилегированные read-only контейнеры, cap-drop, ресурсные бюджеты; backend network internal. LAN listen-all/any-source и MSS — явные сохраняемые настройки IPv4; nft управляет только своей таблицей, MSS ограничен SYN веб-порта. Custom system files/env отказывают в небезопасных случаях; `.env`, demo manifest, медиа и артефакты исключены из Git/образа. Логи не выводят пароли, cookie, CSRF или тела HTTP. |

Ранее найденные ошибки инициализации moderation после мягкой навигации и `recommendations.items` в watch template исправлены до этого итогового прохода; актуальные lifecycle и `recommendations['items']` повторно проверены. Они не являются открытыми замечаниями текущего снимка.

## Собственные выполненные проверки

1. Read-only Docker `foxtube-dev:local`, `--network none`, `--read-only`, `--cap-drop ALL`, `no-new-privileges`, только tmpfs `/tmp`; актуальные app/tests/scripts монтировались read-only, `.env`, Docker socket и реальные данные не подключались. Узкий набор auth unit, canonical origin, request limits, web и части transcode: **56 passed, 14 deselected**. Лог: `.artifacts/audit-2026-11-11/security-unit.log`.
2. В такой же изоляции полный `tests/test_transcode.py` с настоящими FFmpeg/ffprobe: **25 passed** за 11.86 s. Включены неверный файл, внешний HLS/ffconcat, лимиты и отсутствие секретов в окружении, ограничение stdout/stderr, timeout/reap, запрет перезаписи и отказ обрезанного MP4. Лог: `.artifacts/audit-2026-11-11/security-transcode.log`. Наборы частично пересекаются; их количества не складываются в число уникальных тестов.
3. Реальный `recommendations.js` в Node VM: **7/7 passed** — baseline один переход; cancel, disable, session change, hidden, navigation и close запрещают переход даже при позднем fetch, игнорирующем AbortSignal. Исполняемый probe: `.artifacts/audit-2026-11-11/security-autoplay-probe.mjs`; результат с SHA-256 источника: `security-autoplay.json`. Команда из корня репозитория: `node .artifacts/audit-2026-11-11/security-autoplay-probe.mjs`.
4. Изолированный `ffmpeg -h demuxer=mov`: в используемом образе `enable_drefs` и `use_absolute_path` по умолчанию false. Реальные внешние сервисы и приватные файлы не использовались.
5. Проверены имена tracked-файлов для env/key/archive: реальная `.env`, ключи, demo manifest, приватные файлы и `.artifacts` в индекс не включены. Содержимое реальных секретов не читалось. Просмотрены правила исключений и пути записи диагностик.

Оба pytest-прогона имеют одно предупреждение FastAPI/Starlette о deprecated httpx TestClient; падений нет.

## Проверки других участников

По сообщению root от 2026-11-11: `make check` — 294 unit passed; `make test` — 100 PostgreSQL/Nginx integration passed. QA Chrome — 156 passed, 29 Range 206, JS errors 0. Эти проверки не запускались повторно ревьюером и не заменяют независимые результаты выше.

Root также выполнил frozen export и pip-audit точных версий: 29 runtime + 66 development packages, известных advisory — 0. Отчёты: `.artifacts/audit-2026-11-11/dependencies-runtime.json` и `dependencies-development.json`. Это проверка Python-зависимостей, не всех бинарных компонентов поставки.

## Гипотезы и границы

Конкретных неподтверждённых гипотез с воспроизводимым путём эксплуатации после проверки не осталось. Новых конфигурационных или продуктовых ограничений аудит не предлагает.

Ревьюер не повторял полный PostgreSQL/Nginx/Chrome прогон, capacity/resilience и установку на чистую Ubuntu; их итог ведут root/QA/инфраструктура. Собственные динамические проверки не измеряют большой поток запросов или истощение диска. Node probe использует synthetic DOM/fetch/timers и guarded double `playNext`: не проверяет реальный mini DOM commit, media decoder и отсчёт по настенным часам. ACL в этом проходе самостоятельно прослежен статически; реальные HEAD/Range/revoke и браузер проверялись указанными внешними прогонами.

Не выполнялся новый независимый advisory scan системных пакетов FFmpeg/Nginx/PostgreSQL и bundled Swagger UI; вывод «0 advisory» выше относится только к сообщённому pip-audit. Не проверялись реальные приватные медиа, содержимое секретов, внешние сервисы или злоумышленник с уже полученными правами root/DB owner. Фактическую текущую конфигурацию удалённого сервера этот кодовый аудит не аттестует. Авторизованный LAN HTTP/all-source режим принят как решение пользователя; публичная HTTPS-поставка остаётся отдельным условием из security.md.

Отчёт относится к зафиксированным хешам исходников. Последующие изменения авторизации, медиа или async guard требуют отдельного просмотра.

## Дополнительный просмотр исправления nft ownership

После исходного снимка инфраструктурный аудит обнаружил и исправил **P2: неполная проверка владения nft table**. Условие: к управляемой `inet foxtube_lan` с корректно помеченным правилом администратор добавляет собственную пустую chain либо named set/map/counter/quota; прежний validator проверял правила и мог разрешить managed reload таблицы с чужими объектами. Это нарушало обещание сохранения custom-конфигурации. Исправление выполнено инфраструктурным агентом; открытого замечания по текущей версии нет.

Независимо просмотрены только новый `validate_owned_table` и связанные тесты. `scripts/configure_lan.py:321–355` теперь проверяет каждый объект JSON: ровно одну ожидаемую table, точные характеристики ingress и опциональной tcp_mss chain, без неизвестных объектов, дополнительных полей или дубликатов. `scripts/configure_lan.py:356–377` сохраняет проверку маркеров/числа правил и исключение только для точного legacy reply. Пустая tcp_mss chain также не принимается без единственного управляемого правила.

Порядок отказа прослежен статически: вызов validator на строке 535 предшествует `nft --check` (567), всем постоянным `write_system_file` (572–574), systemctl (575–577) и замене `.env` (582). До него создаётся только временный кандидат env для Compose config; он удаляется в finally. При обнаружении чужого объекта ValueError выходит из configure до постоянной записи или reload.

Регрессия: `tests/test_lan.py:650` проверяет отказ для chain/set/map/counter/quota без изменения входных данных; строка 662 — изменённые hook/prio/policy/family; строка 891 — четыре комбинации exact legacy/marked reply и MSS. В независимом read-only Docker без сети **197 tests/test_lan.py passed**. Лог: `.artifacts/audit-2026-11-11/security-nft-ownership.log`. Отдельные хеши этого follow-up сохранены в `security-nft-ownership.json`, исходный снимок не заменён.

Новых подтверждённых P0–P3 не найдено. Реальный nft и сохранность объектов при отказе в Ubuntu проверяет инфраструктурный агент; этот follow-up подтверждает код, unit-регрессию и порядок вызовов, не выдаёт чужой runtime-прогон за собственный.
