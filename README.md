# truyenAI

MVP sản xuất truyện tiếng Việt từ một prompt: Next.js dashboard, FastAPI, một StoryAgent nhiều giai đoạn, Gemini qua OpenAI-compatible API, PostgreSQL và Redis/arq.

## Khởi chạy

Cần Docker + Docker Compose. Các cổng chỉ bind localhost, không public Internet.

```sh
cp .env.example .env  # chỉ chạy lần đầu; không ghi đè .env đã cấu hình
make up
```

- Dashboard: http://localhost:13000
- API/OpenAPI: http://localhost:18080/docs
- Health: http://localhost:18080/api/health
- PostgreSQL: localhost:54329; Redis: localhost:63790

Cổng 8000 trên máy hiện tại đang thuộc dịch vụ khác, nên API dùng 18080. Không dừng hay sửa dịch vụ khác.

Không có key: chạy mock/demo có nhãn rõ ràng. Đây là nội dung cố định để kiểm tra plumbing, không phải truyện do Gemini viết, không bám mọi yêu cầu prompt và không đạt độ dài yêu cầu. Lỗi Gemini thật KHÔNG tự chuyển sang demo.

## Bật Gemini

Tự sửa file `.env` trên máy, không đưa key vào chat/git/frontend:

```dotenv
LLM_MODE=live
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_MODEL=gemini-2.5-flash
LLM_API_KEY=<key của bạn>
LLM_MAX_TOKENS=16000
```

Lấy key tại https://aistudio.google.com/apikey. Model là cấu hình, đổi sang model Gemini được cấp quyền trong tài khoản nếu cần. Endpoint tương thích được xác nhận tại https://ai.google.dev/gemini-api/docs/openai. Sau khi đổi:

```sh
docker compose up -d --force-recreate api worker
```

Health phải hiện `llm_mode: live`. Tạo truyện MỚI để thử provider thật: một truyện đã checkpoint ở demo sẽ giữ nguyên các bước đó khi resume. Đừng đổi provider giữa một tác vụ. Bắt đầu 1–2 chương ngắn để kiểm tra quota/chi phí. Chưa có bằng chứng kiểm thử Gemini thật nếu chưa cung cấp key.

`LLM_MODE=auto` (mặc định): key rỗng -> demo; có key -> live. `mock`: luôn demo. `live`: bắt buộc key. Chỉ API/worker đọc key. Không log response body/header chứa key.

## Sử dụng

1. Tạo truyện, nhập prompt mô tả thể loại, premise, nhân vật, thế giới, giọng văn, số chương, độ dài và kết thúc. Chỉ prompt là đủ; số chương/số từ trong tùy chọn chỉ ghi đè khi bạn điền rõ.
2. Dashboard hiển thị outline, story bible, nhân vật, chương và tiến độ SSE.
3. Có thể hủy; kết quả LLM đang bay sẽ bị từ chối ghi khi trở về. Chọn tiếp tục khi hủy/lỗi; checkpoint đã hoàn tất không gọi LLM lại.
4. Tác vụ hoàn tất cho phép sửa tiêu đề/nội dung chương, xem toàn bộ revisions và tải Markdown. Sửa tay tạo revision mới; bản đánh giá AI trước bị đánh dấu không còn hiệu lực.
5. `needs_review` nghĩa là còn lỗi sau số vòng sửa giới hạn hoặc nội dung đã sửa tay; không có lời hứa truyện chắc chắn không còn plot hole. Job `done` không đồng nghĩa vượt qua QA.

## Agent và độ bền

Một `StoryAgent` điều phối tuần tự, không phải nhiều chatbot độc lập:

```
prompt -> analyze -> outline -> bible/characters
       -> [write N -> review -> bounded revise -> memory N] từng chương
       -> final_review -> bounded targeted repairs + refresh memory
       -> completed hoặc needs_review
```

- Spec trích cấu trúc từ prompt; JSON được Pydantic validate. Outline bắt buộc đủ chương và đánh số liên tiếp.
- Context mỗi chương gồm bible, outline, bộ nhớ sự kiện/trạng thái nhân vật/tuyến bỏ ngỏ của chương trước và toàn văn chương hiện tại khi review. Không gửi lại toàn văn mọi chương.
- Review tổng kết dùng bộ nhớ tất cả chương + toàn văn chương kết, không phải kiểm tra mọi câu của toàn truyện. Truyện dài có thể vượt context; MVP giới hạn 30 chương/5.000 từ mỗi chương.
- PostgreSQL lưu project/spec/bible/final_review; bảng characters, outline, chapters, revisions, jobs, agent_steps, agent_events. Bible nằm JSON trên project thay vì bảng riêng.
- Mỗi checkpoint và dữ liệu phát sinh commit cùng transaction. Request LLM xảy ra ngoài transaction để không giữ row lock lâu. Nếu crash sau provider response nhưng trước commit, lần chạy sau có thể phải trả phí lại cho bước đó.
- PostgreSQL advisory lock chống hai worker viết cùng truyện; generation token và row lock ngăn kết quả cũ ghi sau cancel/retry.
- Redis/arq giao việc; job đang queued/running trong DB được quét lại mỗi 15 giây, xử lý trường hợp enqueue lỗi hoặc worker crash. Redis bật AOF, dữ liệu Postgres/Redis có named volumes.
- SSE replay/tail từ DB theo event ID (`Last-Event-ID` hoặc `?after=`), không dùng pub/sub dễ mất event khi reconnect. Poll DB mỗi giây khi không có event; phù hợp MVP nhỏ.
- Sửa tay chỉ khi job done để không phá checkpoint/memory của tác vụ đang dở; optimistic `version` chống ghi đè giữa hai tab.
- Không xóa dữ liệu khi `docker compose down`; `down -v` sẽ xóa volume, không dùng nếu chưa backup.

## Phát triển và kiểm thử

Python 3.12 quản lý qua uv, Node 22. Không cài package Python vào system Python 3.14.

```sh
make install
make db-up
make dev-api     # terminal 1
make dev-worker  # terminal 2
make dev-web     # terminal 3

make test       # SQLite tạm, mock, không cần key hoặc Postgres
make lint
make e2e        # yêu cầu stack đang chạy, mặc định từ chối live tránh phát sinh phí
```

Test network tạo project demo thật và giữ lại để xem trong dashboard; báo cáo tại `.artifacts/e2e.json`. Unit tests không thay thế kiểm tra concurrency PostgreSQL; E2E dùng Postgres/Redis thật. `uv.lock` và `package-lock.json` cố định dependency. Migration baseline có version và advisory transaction lock; thay đổi schema tương lai phải thêm migration, `create_all` không tự alter cột.

## Layout

```
backend/app/config.py             cấu hình server-only
backend/app/models.py             mô hình SQLAlchemy
backend/app/migrate.py            versioned baseline migration
backend/app/main.py               API, editor, SSE, export
backend/app/agent/llm.py           Gemini + deterministic mock
backend/app/agent/schemas.py       JSON output contracts
backend/app/agent/workflow.py      agent/checkpoints/revisions
backend/app/worker/__init__.py     arq + recovery + execution lock
backend/tests/                    isolated automated tests
frontend/                         Next.js dashboard tiếng Việt
scripts/e2e.py                    kiểm thử HTTP qua stack thật
```

## Giới hạn MVP / trước khi public

Đây là dashboard admin đơn người dùng ở localhost, CHƯA có login/RBAC/multi-tenant. Không mở cổng công khai. Trước production cần authentication, TLS/reverse proxy, secrets manager, rate limiting, quota/cost tracking, backup/restore được kiểm chứng và pagination đầy đủ (danh sách hiện tối đa 500 project). Không có accounting token/chi phí hay vòng phê duyệt ngân sách. Hủy không hoàn lại chi phí request đang chạy. Nội dung truyện và prompt sẽ gửi đến LLM provider khi bật live.

Agent không bảo đảm chất lượng văn học, dung lượng chính xác hoặc không còn lỗi; cần người đọc duyệt. Vòng sửa mặc định tối đa 2 cho mỗi chương và 2 vòng toàn truyện. Tác vụ arq có timeout 2 giờ; khi bị gián đoạn, bộ quét có thể giao lại và resume checkpoint. Chưa có upload tài liệu, RAG/vector DB, hình, nhạc nền hay xuất DOCX/PDF.

## Truyện ngắn / truyện dài và audio Kokoro-Vietnamese

- Chọn **Truyện ngắn** khi tạo: backend ép đúng 1 chương, kể cả prompt/LLM yêu cầu nhiều chương. **Truyện dài** giữ nguyên suy luận/tùy chọn 1–30 chương. Project cũ vẫn giữ nguyên.
- Kokoro-Vietnamese chạy bằng torch cục bộ (CPU hoặc CUDA), không gửi nội dung audio ra dịch vụ TTS ngoài. G2P tiếng Việt bằng `vig2p`. 9router chỉ dùng cho viết truyện.
- Cài lần đầu: `make install && make tts-model`. Model + voicepack (~82M base) tải từ HuggingFace (`contextboxai/Kokoro-Vietnamese`) vào HF cache; `scripts/prepare_kokoro.py` tải sẵn và kiểm tra môi trường. Package được vendor sẵn ở `backend/kokoro_vietnamese/` (không phải dep pip).
- `docker compose up -d --build`: MP3 lưu named volume `audio`, dùng chung API/worker. Không cần GPU. Host native cần ffmpeg; image Docker đã có.
- Trong chương đã hoàn tất: lưu chỉnh sửa trước, chọn một trong **14 giọng tiếng Việt** (mặc định `diem_trinh`), bấm **Chuyển audio**. Theo dõi queued/processing/ready, nghe và tải MP3. Đổi giọng và tạo lại được. Không hỗ trợ clone giọng ở phiên bản này.
- Audio gắn với version nội dung. Sửa chương sẽ đánh dấu audio cũ là stale và chặn tải nhầm; đang tổng hợp mà sửa thì kết quả cũ không được công bố.
- Kokoro tự chia câu và khớp crossfade giữa đoạn trong một lời gọi; output 24 kHz PCM rồi mã hóa MP3, tránh giữ toàn truyện trong RAM. Nhẹ VRAM (~0.7 GB) và nhanh hơn nhiều so với ZeroTTS (dữ liệu benchmark: GPU RTF ~0.03, CPU RTF ~0.38).
- Các tác vụ TTS được tuần tự hóa bằng PG advisory lock để không nạp/sử dụng model đồng thời. Một instance model được cache cho mỗi giọng (giới hạn 3 để giữ VRAM). Đây là TTS đơn giọng từng chương, chưa ghép sách/nhạc nền/đa giọng.
- `GET /api/audio/voices`; `POST /api/projects/{id}/chapters/{n}/audio?voice=diem_trinh`; `GET .../audio/status`; `GET .../audio` hỗ trợ Range cho player.
- Với DB v1/v2 sẵn có, migration v3 bổ sung generation/version/error cho audio. Tác vụ audio thử nghiệm cũ bị gián đoạn được đánh dấu failed và có thể retry.
- `scripts/verify_audio.py` đọc một chương đã tồn tại, tạo audio Kokoro thật và kiểm tra tải MP3/HTTP Range, không gọi LLM. Truyền `AUDIO_PROJECT_ID` để chọn project của bạn. Test này tốn CPU/RAM hoặc GPU/VRAM, không chạy cùng nhiều tác vụ inference.

### Video từ audio (FFmpeg)

- Chương đã có audio sẵn sàng có thể render thành video MP4 nền: scale/pad về 1920x1080 (H.264 yuv420p + AAC, `-movflags +faststart`), dùng `-stream_loop -1` nền + `-shortest`, gắn với `audio_version`/`audio_generation`. Sửa chương hoặc tạo audio mới sẽ đánh dấu video cũ là stale và chặn tải nhầm; đang render mà input đổi thì kết quả cũ không được công bố.
- Thư mục nền `data/video-backgrounds` (mount read-only vào API/worker) chứa file .mp4/.mov/.mkv/.webm 1080p. MP4 xuất ra named volume `videos`; host native cần ffmpeg (image Docker đã có).
- API: `GET /api/video/backgrounds`; `POST /api/projects/{id}/chapters/{n}/video {background?}`; `GET .../video/status`; `GET .../video` (Range). Render chạy vụ worker với PG advisory lock và generation fence, như audio.
- `scripts/verify_video.py` tạo thật một video từ chương đã có audio + nền testsrc (không gọi LLM), ffprobe xác nhận 1920x1080/AAC/duration≈audio, ghi file vào `.artifacts/chapter-video.mp4`.

### Xuất bản TikTok / YouTube

- Kết nối tài khoản qua OAuth: `GET /api/social/{platform}/authorize` (tiktok|youtube) trả URL; sau khi người dùng đồng ý, browser được redirect về `/publish?connected={platform}` hoặc `?error=...`. `DELETE /api/social/{platform}` ngắt kết nối; `GET /api/social/status` cho biết trạng thái.
- Cần tạo TikTok app (Content Posting API, scopes `video.publish,user.info.basic`) và Google OAuth client (YouTube Data API v3, scopes `youtube.upload,youtube.readonly`) rồi điền `TIKTOK_CLIENT_KEY/SECRET`, `YOUTUBE_CLIENT_ID/SECRET`, `SOCIAL_REDIRECT_BASE` trong `.env`. Đăng bài LIVE đòi hỏi app TikTok được duyệt (audit); chưa duyệt thì chỉ kiểm thử bằng HTTP mock.
- `POST /api/videos/{id}/publish` (caption/hashtags/privacy cho TikTok; title/description/tags/category_id/privacy/schedule_at cho YouTube), theo dõi `GET /api/publishes/{id}`, `POST /api/publishes/{id}/retry`. Upload chạy vụ worker, token được refresh tự động (YouTube), generation fence tránh đăng đè sau khi chỉnh sửa.
- Dữ liệu tài khoản xã hội và lịch sử đăng nằm trong DB (`social_accounts`, `publishes`); không log token. Không có test live nếu chưa có app duyệt.

### GPU NVIDIA (tùy chọn)

- Đã kiểm tra CUDA trên RTX 2060 6GB (driver 595.84 / CUDA 13.2). TTS dùng torch (PyPI cấp wheel GPU-capable cu13), không cần onnxruntime-gpu riêng. Chỉ hỗ trợ `cpu` và `cuda`.
- Bật GPU: `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build worker`. Các lần cập nhật worker sau cũng phải dùng cả hai file. API/Postgres/Redis không cần GPU.
- Trở về CPU: `docker compose -f docker-compose.yml up -d --build --force-recreate worker` (để `TTS_DEVICE=cpu` hoặc không đặt trong .env).
- CPU vẫn là mặc định. File GPU override đặt `TTS_DEVICE=cuda`; `TTS_HOST_GPU_ID=0` chọn GPU vật lý. Container chỉ thấy một GPU nên `TTS_GPU_DEVICE_ID=0` ở bên trong. Chạy native thì chỉ số này là chỉ số GPU của process.
- Kokoro rất nhẹ VRAM (~0.7 GB); khi chưa có CUDA khả dụng thì tự rơi về CPU. Lỗi tải/thiếu model báo rõ, không tự gửi nội dung đi đâu.
- Giữ một tác vụ TTS mỗi lần (PG advisory lock) và cache model giới hạn 3 giọng để tránh tràn VRAM. Không cam kết GPU luôn nhanh hơn cho mọi văn bản.
