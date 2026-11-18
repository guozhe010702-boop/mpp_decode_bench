// Native IMX415 -> MPP encode -> MPP decode -> DRM atomic scanout.
// Encoder layout/copy follows ../examples/mpp_c_api and the existing
// Low latency stream encoder. Every pending raw/display queue has capacity one.
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <csignal>
#include <cstring>
#include <deque>
#include <fstream>
#include <functional>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <numeric>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <tuple>
#include <vector>
#include <fcntl.h>
#include <linux/videodev2.h>
#include <linux/dma-buf.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <xf86drm.h>
#include <xf86drmMode.h>
#include <drm_fourcc.h>
extern "C" {
#include <rk_mpi.h>
#include <mpp_buffer.h>
#include <mpp_frame.h>
#include <mpp_packet.h>
#include <rk_venc_cfg.h>
#include <rk_venc_cmd.h>
#include <rk_venc_rc.h>
}

using Clock = std::chrono::steady_clock;
using Microseconds = std::chrono::microseconds;
static volatile sig_atomic_t interrupted = 0;
static std::string lifecycle_path;
static std::mutex lifecycle_mu;
static int64_t now_us() {
    timespec t{}; clock_gettime(CLOCK_MONOTONIC, &t);
    return int64_t(t.tv_sec) * 1000000 + t.tv_nsec / 1000;
}
static void lifecycle(const char* event) noexcept {
    try {
        std::lock_guard<std::mutex> lock(lifecycle_mu);
        int fd=::open(lifecycle_path.c_str(),O_WRONLY|O_CREAT|O_APPEND|O_DSYNC|O_CLOEXEC,0644);
        if(fd<0) return;
        char line[256];int n=snprintf(line,sizeof(line),"%lld %s\n",static_cast<long long>(now_us()),event);
        if(n>0) {ssize_t ignored=write(fd,line,std::min(n,int(sizeof(line)-1)));(void)ignored;}
        close(fd);
    } catch(...) {}
}
static void check(bool ok, const std::string& what) {
    if (!ok) throw std::runtime_error(what + ": " + strerror(errno));
}
static void mcheck(MPP_RET r, const std::string& what) {
    if (r != MPP_OK) throw std::runtime_error(what + " MPP_RET=" + std::to_string(r));
}
static int xioctl(int fd, unsigned long op, void* p) {
    int r; do { r = ioctl(fd, op, p); } while (r < 0 && errno == EINTR); return r;
}
struct Options {
    std::string device = "/dev/video44", codec = "hevc", out, overlay_file;
    std::string display_scheduling = "immediate";
    int buffers = 6, seconds = 30, warmup = 5, bitrate = 20000000;
    int immediate = 1, low_delay = 1, max_age_ms = 100;
    int stall_ms = 0, stall_after = 10, split_ctus = 0, display_error_after = 0;
    int display_margin_us = 2500;
};
static Options options(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        std::string k = argv[i];
        if (i + 1 >= argc) throw std::runtime_error("Missing value: " + k);
        std::string v = argv[++i];
        if (k == "--codec") o.codec = v;
        else if (k == "--out") o.out = v;
        else if (k == "--device") o.device = v;
        else if (k == "--overlay-file") o.overlay_file = v;
        else if (k == "--display-scheduling") o.display_scheduling = v;
        else if (k == "--display-margin-us") o.display_margin_us = std::stoi(v);
        else if (k == "--buffers") o.buffers = std::stoi(v);
        else if (k == "--seconds") o.seconds = std::stoi(v);
        else if (k == "--warmup") o.warmup = std::stoi(v);
        else if (k == "--bitrate") o.bitrate = std::stoi(v);
        else if (k == "--immediate-out") o.immediate = std::stoi(v);
        else if (k == "--low-delay") o.low_delay = std::stoi(v);
        else if (k == "--max-age-ms") o.max_age_ms = std::stoi(v);
        else if (k == "--inject-stall-ms") o.stall_ms = std::stoi(v);
        else if (k == "--stall-after") o.stall_after = std::stoi(v);
        else if (k == "--split-ctus") o.split_ctus = std::stoi(v);
        else if (k == "--inject-display-error-after") o.display_error_after = std::stoi(v);
        else throw std::runtime_error("Unknown argument: " + k);
    }
    if (o.out.empty() || (o.codec != "hevc" && o.codec != "h264" && o.codec != "direct") ||
        o.buffers < 3 || o.buffers > 32 || o.seconds < 0 || o.warmup < 0 ||
        o.bitrate < 100000 || o.bitrate > 100000000 || o.max_age_ms < 1 ||
        (o.display_scheduling != "immediate" && o.display_scheduling != "deadline") ||
        o.display_margin_us < 500 || o.display_margin_us > 8000)
        throw std::runtime_error("Invalid options");
    return o;
}
struct Trace {
    uint64_t id = 0; uint32_t sequence = 0, flags = 0;
    int64_t driver = 0, dq = 0, copy_start = 0, copy_end = 0;
    int64_t enc_in = 0, enc_out = 0, dec_in = 0, dec_out = 0;
    int64_t display_in = 0, flip_event = 0, flip_received = 0, display_target = 0;
};
using TracePtr = std::shared_ptr<Trace>;
struct Counters {
    std::atomic<uint64_t> captured{0}, encoded{0}, decoded{0}, displayed{0}, bytes{0};
    std::atomic<uint64_t> raw_drops{0}, display_drops{0}, stale_drops{0}, gaps{0}, startup_gaps{0}, errors{0};
    std::atomic<int64_t> last_latency_us{0};
    std::atomic<uint64_t> missed_targets{0};
    std::atomic<int64_t> max_overlay_prepare_us{0};
};
class Records {
    std::mutex mu;
    std::ofstream csv;
    int64_t start; int warmup;
    struct Histogram {
        // Fixed memory even for continuous manual preview. Upper-edge bins.
        std::vector<uint64_t> bins=std::vector<uint64_t>(20001,0);
        uint64_t n=0; double maximum=0;
        void add(double ms) {++n;maximum=std::max(maximum,ms);++bins[std::min(size_t(20000),size_t(std::ceil(ms*10)))];}
        double percentile(double q) const {
            uint64_t target=std::max(uint64_t(1),uint64_t(std::ceil(q*n))),sum=0;
            for(size_t i=0;i<bins.size();++i) {sum+=bins[i];if(sum>=target) return i==20000?maximum:i/10.0;}
            return maximum;
        }
    };
    std::map<std::string, Histogram> samples;
public:
    Records(const Options& o, int64_t s) : csv(o.out + "/frames.csv"), start(s), warmup(o.warmup) {
        check(bool(csv), "open frames.csv");
        csv << "id,sequence,flags,driver_us,dq_us,copy_start_us,copy_end_us,enc_in_us,enc_out_us,dec_in_us,dec_out_us,display_in_us,flip_event_us,flip_received_us,display_target_us,outcome\n";
    }
    void record(const TracePtr& t, const char* outcome) {
        if (!t) return;
        std::lock_guard<std::mutex> lock(mu);
        csv << t->id << ',' << t->sequence << ',' << t->flags << ',' << t->driver << ',' << t->dq
            << ',' << t->copy_start << ',' << t->copy_end << ',' << t->enc_in << ',' << t->enc_out
            << ',' << t->dec_in << ',' << t->dec_out << ',' << t->display_in << ',' << t->flip_event
            << ',' << t->flip_received << ',' << t->display_target << ',' << outcome << '\n';
        if (std::string(outcome) != "displayed" || t->dq < start + int64_t(warmup) * 1000000) return;
        auto add = [&](const char* name, int64_t a, int64_t b) {
            if (a && b >= a) samples[name].add((b - a) / 1000.0);
        };
        add("dq_to_flip_ms", t->dq, t->flip_event);
        add("dq_to_flip_received_ms", t->dq, t->flip_received);
        add("raw_wait_ms", t->dq, t->copy_start);
        add("layout_copy_ms", t->copy_start, t->copy_end);
        add("encode_ms", t->enc_in, t->enc_out);
        add("decode_ms", t->dec_in, t->dec_out);
        add("display_wait_ms", t->dec_out ? t->dec_out : t->dq, t->display_in);
        add("kms_flip_ms", t->display_in, t->flip_event);
        add("commit_lead_ms", t->display_in, t->display_target);
        if ((t->flags & V4L2_BUF_FLAG_TIMESTAMP_MASK) == V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC &&
            t->driver <= t->dq && t->dq - t->driver < 1000000)
            add("driver_timestamp_to_flip_ms", t->driver, t->flip_event);
    }
    void summary(std::ostream& out) {
        std::lock_guard<std::mutex> lock(mu);
        out << '{'; bool first = true;
        for (auto& kv : samples) {
            auto& v = kv.second; if (!v.n) continue;
            if (!first) out << ',';
            first = false;
            out << '"' << kv.first << "\":{\"n\":" << v.n << ",\"p50\":" << v.percentile(.5)
                << ",\"p95\":" << v.percentile(.95) << ",\"p99\":" << v.percentile(.99)
                << ",\"max\":" << v.maximum << ",\"quantile_resolution_ms\":0.1}";
        } out << '}'; csv.flush();
    }
};
template<class T> class Latest {
    std::mutex mu; std::condition_variable cv; std::shared_ptr<T> item;
public:
    std::shared_ptr<T> put(std::shared_ptr<T> value) {
        std::shared_ptr<T> old;
        { std::lock_guard<std::mutex> lock(mu); old.swap(item); item = std::move(value); }
        cv.notify_one(); return old;
    }
    std::shared_ptr<T> get() {
        std::unique_lock<std::mutex> lock(mu);
        cv.wait_for(lock, std::chrono::milliseconds(100), [&] { return bool(item); });
        auto result = std::move(item); return result;
    }
    std::shared_ptr<T> get_until(int64_t deadline_us) {
        std::unique_lock<std::mutex> lock(mu);
        cv.wait_for(lock, std::chrono::milliseconds(100), [&] { return bool(item); });
        if(!item) return {};
        // Keep the capacity-one slot replaceable until the submission deadline.
        // A newly decoded frame can displace an older candidate while we wait.
        while(!interrupted) {
            int64_t remaining=deadline_us-now_us();
            if(remaining<=0) break;
            cv.wait_for(lock,Microseconds(remaining));
        }
        return std::move(item);
    }
    std::shared_ptr<T> drain() { std::lock_guard<std::mutex> lock(mu); return std::move(item); }
};

class Camera;
struct Raw {
    Camera* camera = nullptr; unsigned index = 0;
    std::array<unsigned, 2> used{}, offset{}; TracePtr trace;
    ~Raw();
};
class Camera {
    struct Plane { void* ptr = MAP_FAILED; size_t length = 0; int dma = -1; };
    std::vector<std::array<Plane,2>> slots;
    int fd = -1; bool streaming = false;
    std::mutex queue_mu;
    v4l2_format saved{};
public:
    std::array<unsigned,2> stride{};
    unsigned count = 0;
    Camera() = default;
    void open(const Options& o) {
        fd = ::open(o.device.c_str(), O_RDWR | O_NONBLOCK | O_CLOEXEC); check(fd >= 0, "open camera");
        saved.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
        check(xioctl(fd, VIDIOC_G_FMT, &saved) == 0, "save camera format");
        v4l2_format f = saved;
        f.fmt.pix_mp.width = 3840; f.fmt.pix_mp.height = 2160;
        f.fmt.pix_mp.pixelformat = V4L2_PIX_FMT_NV12M; f.fmt.pix_mp.field = V4L2_FIELD_NONE;
        check(xioctl(fd, VIDIOC_S_FMT, &f) == 0, "set 4K NV12M");
        check(f.fmt.pix_mp.width == 3840 && f.fmt.pix_mp.height == 2160 &&
              f.fmt.pix_mp.pixelformat == V4L2_PIX_FMT_NV12M && f.fmt.pix_mp.num_planes == 2,
              "camera must return 4K NV12M / two planes");
        for (int p=0;p<2;++p) stride[p]=f.fmt.pix_mp.plane_fmt[p].bytesperline;
        v4l2_requestbuffers r{}; r.type=saved.type; r.memory=V4L2_MEMORY_MMAP; r.count=o.buffers;
        check(xioctl(fd, VIDIOC_REQBUFS, &r)==0, "request camera buffers");
        count=r.count; slots.resize(count);
        for (unsigned i=0;i<count;++i) {
            v4l2_plane ps[2]{}; v4l2_buffer b{}; b.type=saved.type; b.memory=V4L2_MEMORY_MMAP;
            b.index=i; b.length=2; b.m.planes=ps;
            check(xioctl(fd, VIDIOC_QUERYBUF, &b)==0, "query camera buffer");
            for (int p=0;p<2;++p) {
                auto& s=slots[i][p]; s.length=ps[p].length;
                s.ptr=mmap(nullptr,s.length,PROT_READ|PROT_WRITE,MAP_SHARED,fd,ps[p].m.mem_offset);
                check(s.ptr!=MAP_FAILED,"mmap camera plane");
                v4l2_exportbuffer e{}; e.type=saved.type; e.index=i; e.plane=p; e.flags=O_CLOEXEC;
                check(xioctl(fd,VIDIOC_EXPBUF,&e)==0,"export camera plane"); s.dma=e.fd;
            }
            requeue(i);
        }
        std::cerr << "CAMERA 3840x2160 NM12 buffers=" << count << " strides=" << stride[0] << ',' << stride[1] << '\n';
    }
    void start() { auto type=saved.type; check(xioctl(fd,VIDIOC_STREAMON,&type)==0,"camera STREAMON"); streaming=true; }
    void requeue(unsigned i) {
        std::lock_guard<std::mutex> lock(queue_mu);
        v4l2_plane ps[2]{}; v4l2_buffer b{}; b.type=saved.type; b.memory=V4L2_MEMORY_MMAP;
        b.index=i; b.length=2; b.m.planes=ps;
        check(xioctl(fd,VIDIOC_QBUF,&b)==0,"camera QBUF");
    }
    std::shared_ptr<Raw> take() {
        pollfd p{fd,POLLIN,0}; int n=poll(&p,1,200);
        if (n<0 && errno==EINTR) return {};
        check(n>=0,"camera poll"); if(!n) return {};
        v4l2_plane ps[2]{}; v4l2_buffer b{}; b.type=saved.type; b.memory=V4L2_MEMORY_MMAP;
        b.length=2; b.m.planes=ps;
        if(xioctl(fd,VIDIOC_DQBUF,&b)<0) { if(errno==EAGAIN) return {}; check(false,"camera DQBUF"); }
        auto r=std::make_shared<Raw>(); r->camera=this; r->index=b.index; r->trace=std::make_shared<Trace>();
        r->trace->dq=now_us(); r->trace->sequence=b.sequence; r->trace->flags=b.flags;
        r->trace->driver=int64_t(b.timestamp.tv_sec)*1000000+b.timestamp.tv_usec;
        for(int j=0;j<2;++j) { r->used[j]=ps[j].bytesused; r->offset[j]=ps[j].data_offset;
            check(r->used[j]>=r->offset[j] && r->used[j]<=slots.at(b.index)[j].length,"invalid camera payload"); }
        return r;
    }
    const uint8_t* data(const Raw& r,int p) const { return static_cast<const uint8_t*>(slots[r.index][p].ptr)+r.offset[p]; }
    int dma(const Raw& r,int p) const { return slots[r.index][p].dma; }
    void cpu_sync(const Raw& r,bool start) {
        for(int p=0;p<2;++p) { dma_buf_sync s{}; s.flags=DMA_BUF_SYNC_READ | (start?DMA_BUF_SYNC_START:DMA_BUF_SYNC_END);
            check(ioctl(dma(r,p),DMA_BUF_IOCTL_SYNC,&s)==0,"camera DMA read sync"); }
    }
    ~Camera() {
        if(fd<0) return;
        lifecycle("CAMERA_CLOSE_BEGIN");
        if(streaming) { auto type=saved.type; xioctl(fd,VIDIOC_STREAMOFF,&type); }
        for(auto& slot:slots) for(auto& p:slot) { if(p.ptr!=MAP_FAILED) munmap(p.ptr,p.length); if(p.dma>=0) close(p.dma); }
        v4l2_requestbuffers r{}; r.type=saved.type; r.memory=V4L2_MEMORY_MMAP; xioctl(fd,VIDIOC_REQBUFS,&r);
        if(saved.fmt.pix_mp.width) std::cerr << "CAMERA_FORMAT_RESTORE=" << (xioctl(fd,VIDIOC_S_FMT,&saved)==0) << '\n';
        close(fd);
        lifecycle("CAMERA_CLOSE_DONE");
    }
};
Raw::~Raw() { if(camera) { try { camera->requeue(index); } catch(const std::exception& e) { std::cerr<<e.what()<<'\n'; } } }

struct Picture {
    MppFrame frame=nullptr; std::shared_ptr<Raw> raw; TracePtr trace;
    ~Picture() { if(frame) mpp_frame_deinit(&frame); }
};
class Codec {
public:
    MppCtx enc=nullptr,dec=nullptr; MppApi *ea=nullptr,*da=nullptr;
    MppEncCfg cfg=nullptr; std::array<MppBuffer,2> inputs{};
    std::array<MppFrame,2> frames{}; MppBufferGroup group=nullptr;
    unsigned hs=3840,vs=2160;
    void open(const Options& o) {
        auto type=o.codec=="h264"?MPP_VIDEO_CodingAVC:MPP_VIDEO_CodingHEVC;
        mcheck(mpp_create(&enc,&ea),"create encoder");
        RK_S64 input_timeout=0;
        // This must precede mpp_init: nonblocking input selects MPP async mode.
        // Its default synchronous mode waits for completion inside put_frame.
        mcheck(ea->control(enc,MPP_SET_INPUT_TIMEOUT,&input_timeout),"select asynchronous encoder");
        mcheck(mpp_init(enc,MPP_CTX_ENC,type),"init encoder");
        RK_S64 timeout=1000;
        mcheck(ea->control(enc,MPP_SET_OUTPUT_TIMEOUT,&timeout),"encoder output timeout");
        mcheck(mpp_enc_cfg_init(&cfg),"encoder config"); mcheck(ea->control(enc,MPP_ENC_GET_CFG,cfg),"get encoder config");
        auto set=[&](const char* k,int v) { mcheck(mpp_enc_cfg_set_s32(cfg,k,v),k); };
        set("prep:width",3840); set("prep:height",2160); set("prep:hor_stride",hs); set("prep:ver_stride",vs);
        set("prep:format",MPP_FMT_YUV420SP); set("rc:mode",MPP_ENC_RC_MODE_CBR);
        set("rc:bps_target",o.bitrate); set("rc:bps_min",o.bitrate*9/10); set("rc:bps_max",o.bitrate*11/10);
        set("rc:fps_in_flex",0); set("rc:fps_in_num",60); set("rc:fps_in_denom",1);
        set("rc:fps_out_flex",0); set("rc:fps_out_num",60); set("rc:fps_out_denom",1); set("rc:gop",60);
        set("rc:max_reenc_times",0); set("rc:drop_mode",0); set("base:low_delay",o.low_delay);
        set("prep:colorspace",MPP_FRAME_SPC_BT709); set("prep:colorprim",1);set("prep:colortrc",1);
        set("prep:colorrange",MPP_FRAME_RANGE_JPEG);set("prep:range_out",MPP_FRAME_RANGE_JPEG);
        if(o.codec=="h264") { set("h264:profile",100); set("h264:level",52); set("h264:cabac_en",1); }
        if(o.split_ctus) { set("split:mode",2); set("split:arg",o.split_ctus); set("split:out",1); }
        mcheck(ea->control(enc,MPP_ENC_SET_CFG,cfg),"set encoder config");
        mcheck(ea->control(enc,MPP_ENC_GET_CFG,cfg),"read encoder config back");
        std::ofstream config(o.out+"/mpp-config.txt");
        for(auto k:{"prep:width","prep:height","rc:fps_in_num","rc:fps_out_num","rc:gop","rc:bps_target","rc:max_reenc_times","base:low_delay"}) {
            RK_S32 value=0; mcheck(mpp_enc_cfg_get_s32(cfg,k,&value),k); config<<k<<'='<<value<<'\n'; }
        MppEncHeaderMode h=MPP_ENC_HEADER_MODE_EACH_IDR;
        mcheck(ea->control(enc,MPP_ENC_SET_HEADER_MODE,&h),"headers each IDR");
        mcheck(mpp_buffer_group_get_internal(&group,MPP_BUFFER_TYPE_DRM),"input DRM group");
        for(auto& input:inputs) mcheck(mpp_buffer_get(group,&input,size_t(hs)*vs*3/2),"encoder input buffer");
        for(size_t i=0;i<frames.size();++i) {
            mcheck(mpp_frame_init(&frames[i]),"persistent encoder frame");
            mpp_frame_set_width(frames[i],3840);mpp_frame_set_height(frames[i],2160);
            mpp_frame_set_hor_stride(frames[i],hs);mpp_frame_set_ver_stride(frames[i],vs);
            mpp_frame_set_fmt(frames[i],MPP_FMT_YUV420SP);mpp_frame_set_buffer(frames[i],inputs[i]);
        }
        mcheck(mpp_create(&dec,&da),"create decoder");
        RK_U32 split=0, fast=1, immediate=o.immediate;
        mcheck(da->control(dec,MPP_DEC_SET_PARSER_SPLIT_MODE,&split),"complete AU decoder input");
        mcheck(da->control(dec,MPP_DEC_SET_PARSER_FAST_MODE,&fast),"decoder fast parser");
        if(o.codec=="h264") mcheck(da->control(dec,MPP_DEC_SET_IMMEDIATE_OUT,&immediate),"H264 immediate output");
        mcheck(mpp_init(dec,MPP_CTX_DEC,type),"init decoder");
        timeout=0; mcheck(da->control(dec,MPP_SET_OUTPUT_TIMEOUT,&timeout),"decoder nonblocking output");
    }
    void copy(Camera& camera,const Raw& raw,MppBuffer input) {
        mcheck(mpp_buffer_sync_begin(input),"sync encoder input"); camera.cpu_sync(raw,true);
        auto dst=static_cast<uint8_t*>(mpp_buffer_get_ptr(input));
        for(int p=0;p<2;++p) {
            int rows=p?1080:2160;
            check(camera.stride[p]>=3840 && raw.used[p]-raw.offset[p]>=size_t(rows-1)*camera.stride[p]+3840,"short camera plane");
            auto src=camera.data(raw,p); auto target=dst+(p?size_t(hs)*vs:0);
            if(camera.stride[p]==hs) memcpy(target,src,size_t(hs)*rows);
            else for(int y=0;y<rows;++y) memcpy(target+size_t(y)*hs,src+size_t(y)*camera.stride[p],3840);
        }
        camera.cpu_sync(raw,false); mcheck(mpp_buffer_sync_end(input),"finish encoder input sync");
    }
    ~Codec() {
        if(dec) {lifecycle("MPP_DEC_DESTROY_BEGIN");mpp_destroy(dec);lifecycle("MPP_DEC_DESTROY_DONE");}
        if(enc) {lifecycle("MPP_ENC_DESTROY_BEGIN");mpp_destroy(enc);lifecycle("MPP_ENC_DESTROY_DONE");}
        for(auto& frame:frames) if(frame) mpp_frame_deinit(&frame);
        for(auto& input:inputs) if(input) mpp_buffer_put(input);
        if(group) mpp_buffer_group_put(group);
        if(cfg) mpp_enc_cfg_deinit(cfg);
    }
};

class Display {
    int fd=-1; uint32_t connector=0,crtc=0,plane=0,mode_blob=0; bool active=false;
    uint32_t overlay_plane=0,overlay_revision=0; int overlay_index=0;
    std::string overlay_file;
    bool overlay_dirty=false;
    static constexpr unsigned overlay_width=1152,overlay_height=160;
    struct OverlayBuffer {uint32_t fb=0,handle=0,pitch=0;size_t size=0;void* data=MAP_FAILED;};
    std::array<OverlayBuffer,2> overlay_buffers;
    drmModeModeInfo mode{};
    struct FB { uint32_t id=0; std::vector<uint32_t> handles; };
    std::map<std::tuple<int,int,unsigned,unsigned,unsigned,unsigned>,FB> fbs;
    std::map<std::tuple<uint32_t,uint32_t,std::string>,uint32_t> property_ids;
    struct Flip { bool done=false; int64_t event=0,received=0; } flip;
    uint32_t prop(uint32_t obj,uint32_t type,const char* name) {
        auto key=std::make_tuple(obj,type,std::string(name));
        auto cached=property_ids.find(key);if(cached!=property_ids.end()) return cached->second;
        auto ps=drmModeObjectGetProperties(fd,obj,type); check(ps,"DRM properties"); uint32_t id=0;
        for(unsigned i=0;i<ps->count_props;++i) { auto p=drmModeGetProperty(fd,ps->props[i]);
            if(p) { if(std::string(p->name)==name) id=p->prop_id; drmModeFreeProperty(p); } }
        drmModeFreeObjectProperties(ps); check(id!=0,std::string("DRM property ")+name);
        property_ids.emplace(std::move(key),id);return id;
    }
    void add(drmModeAtomicReq* r,uint32_t obj,uint32_t type,const char* name,uint64_t value) {
        check(drmModeAtomicAddProperty(r,obj,prop(obj,type,name),value)>=0,name);
    }
    static void flipped(int,unsigned,unsigned sec,unsigned usec,void* data) {
        auto f=static_cast<Flip*>(data); f->event=int64_t(sec)*1000000+usec; f->received=now_us(); f->done=true;
    }
    void allocate_overlay() {
        for(auto& b:overlay_buffers) {
            drm_mode_create_dumb create{};create.width=overlay_width;create.height=overlay_height;create.bpp=32;
            check(ioctl(fd,DRM_IOCTL_MODE_CREATE_DUMB,&create)==0,"create status overlay buffer");
            b.handle=create.handle;b.pitch=create.pitch;b.size=create.size;
            drm_mode_map_dumb map{};map.handle=b.handle;
            check(ioctl(fd,DRM_IOCTL_MODE_MAP_DUMB,&map)==0,"map status overlay buffer");
            b.data=mmap(nullptr,b.size,PROT_READ|PROT_WRITE,MAP_SHARED,fd,map.offset);check(b.data!=MAP_FAILED,"mmap status overlay");
            memset(b.data,0,b.size);
            uint32_t hs[4]{b.handle,0,0,0},ps[4]{b.pitch,0,0,0},os[4]{};
            check(drmModeAddFB2(fd,overlay_width,overlay_height,DRM_FORMAT_ARGB8888,hs,ps,os,&b.fb,0)==0,"ARGB overlay framebuffer");
        }
    }
    bool read_overlay() {
        if(overlay_file.empty()) return false;
        std::ifstream file(overlay_file,std::ios::binary);
        struct Header {char magic[4];uint32_t width,height,revision;} h{};
        file.read(reinterpret_cast<char*>(&h),sizeof(h));
        if(!file || memcmp(h.magic,"RKOS",4) || h.width!=overlay_width || h.height!=overlay_height || h.revision==overlay_revision) return false;
        int next=1-overlay_index;auto& b=overlay_buffers[next];
        for(unsigned y=0;y<overlay_height;++y) file.read(static_cast<char*>(b.data)+size_t(y)*b.pitch,overlay_width*4);
        if(!file) return false;
        overlay_revision=h.revision;overlay_index=next;return true;
    }
public:
    bool prepare_overlay() {
        // Only write the inactive buffer once before its flip. If no video
        // frame arrives, keep that pending buffer intact until committed.
        if(overlay_dirty) return false;
        overlay_dirty=read_overlay();return overlay_dirty;
    }
    int64_t period_us() const {
        return std::llround(1000.0*mode.htotal*mode.vtotal/mode.clock);
    }
    int64_t next_target_us() const {return flip.done ? flip.event+period_us() : 0;}
    void open(const Options& o) {
        overlay_file=o.overlay_file;
        fd=::open("/dev/dri/card0",O_RDWR|O_CLOEXEC); check(fd>=0,"open DRM card0");
        check(drmSetMaster(fd)==0,"DRM master (desktop must be stopped)");
        check(drmSetClientCap(fd,DRM_CLIENT_CAP_UNIVERSAL_PLANES,1)==0,"universal planes");
        check(drmSetClientCap(fd,DRM_CLIENT_CAP_ATOMIC,1)==0,"atomic DRM");
        uint64_t mono=0; check(drmGetCap(fd,DRM_CAP_TIMESTAMP_MONOTONIC,&mono)==0 && mono,"monotonic flip timestamps");
        auto res=drmModeGetResources(fd); check(res,"DRM resources"); int ci=-1;
        for(int i=0;i<res->count_connectors && !connector;++i) {
            auto c=drmModeGetConnector(fd,res->connectors[i]);
            if(c && c->connection==DRM_MODE_CONNECTED && c->connector_type==DRM_MODE_CONNECTOR_HDMIA) {
                for(int m=0;m<c->count_modes;++m) { auto& v=c->modes[m];
                    double hz=double(v.clock)*1000/v.htotal/v.vtotal;
                    if(v.hdisplay==1920 && v.vdisplay==1080 && hz>59.5 && hz<60.5 && !(v.flags&DRM_MODE_FLAG_INTERLACE)) {
                        connector=c->connector_id; mode=v; break; } }
                if(connector) for(int e=0;e<c->count_encoders && ci<0;++e) {
                    auto encoder=drmModeGetEncoder(fd,c->encoders[e]); if(encoder) {
                        for(int x=0;x<res->count_crtcs;++x) if(encoder->possible_crtcs & (1u<<x)) { ci=x;crtc=res->crtcs[x];break; }
                        drmModeFreeEncoder(encoder); } }
            } if(c) drmModeFreeConnector(c);
        }
        check(connector && ci>=0,"connected HDMI 1080p60 / CRTC");
        auto planes=drmModeGetPlaneResources(fd); check(planes,"DRM plane resources");
        for(unsigned i=0;i<planes->count_planes && !plane;++i) { auto p=drmModeGetPlane(fd,planes->planes[i]);
            if(p) { if(p->possible_crtcs&(1u<<ci)) for(unsigned f=0;f<p->count_formats;++f)
                    if(p->formats[f]==DRM_FORMAT_NV12) { plane=p->plane_id; break; }
                drmModeFreePlane(p); } }
        if(!overlay_file.empty()) for(unsigned i=0;i<planes->count_planes && !overlay_plane;++i) {
            auto p=drmModeGetPlane(fd,planes->planes[i]);
            if(p) {
                if(p->plane_id!=plane && (p->possible_crtcs&(1u<<ci))) {
                    for(unsigned f=0;f<p->count_formats;++f) {
                        if(p->formats[f]==DRM_FORMAT_ARGB8888) {overlay_plane=p->plane_id;break;}
                    }
                }
                drmModeFreePlane(p);
            }
        }
        drmModeFreePlaneResources(planes); drmModeFreeResources(res);
        check(plane,"NV12 display plane"); check(drmModeCreatePropertyBlob(fd,&mode,sizeof(mode),&mode_blob)==0,"mode blob");
        if(!overlay_file.empty()) {check(overlay_plane,"independent ARGB status plane");allocate_overlay();}
        std::ofstream info(o.out+"/display.json"); info<<"{\"connector\":"<<connector<<",\"crtc\":"<<crtc<<",\"plane\":"<<plane
            <<",\"overlay_plane\":"<<overlay_plane<<",\"width\":1920,\"height\":1080,\"refresh_hz\":60,\"flip_clock\":\"CLOCK_MONOTONIC\"}\n";
    }
    uint32_t framebuffer(const Picture& picture,Camera& camera) {
        int yfd,uvfd; unsigned pitch,vstride,yo=0,uvo;
        if(picture.frame) {
            check(mpp_frame_get_width(picture.frame)==3840 && mpp_frame_get_height(picture.frame)==2160,"decoded frame must be full 4K");
            check(mpp_frame_get_fmt(picture.frame)==MPP_FMT_YUV420SP,"decoder must return linear NV12");
            auto buf=mpp_frame_get_buffer(picture.frame); check(buf,"decoder output buffer");
            yfd=uvfd=mpp_buffer_get_fd(buf); pitch=mpp_frame_get_hor_stride(picture.frame); vstride=mpp_frame_get_ver_stride(picture.frame);
            uvo=pitch*vstride;
        } else {
            yfd=camera.dma(*picture.raw,0); uvfd=camera.dma(*picture.raw,1);
            pitch=camera.stride[0]; vstride=2160; yo=picture.raw->offset[0]; uvo=picture.raw->offset[1];
            check(camera.stride[1]==pitch,"direct display requires equal plane pitches");
        }
        auto key=std::make_tuple(yfd,uvfd,pitch,vstride,yo,uvo); auto found=fbs.find(key); if(found!=fbs.end()) return found->second.id;
        FB b; uint32_t handles[4]{},pitches[4]{pitch,pitch,0,0},offsets[4]{yo,uvo,0,0};
        check(drmPrimeFDToHandle(fd,yfd,&handles[0])==0,"import Y DMA-BUF"); b.handles.push_back(handles[0]);
        if(uvfd==yfd) handles[1]=handles[0];
        else { check(drmPrimeFDToHandle(fd,uvfd,&handles[1])==0,"import UV DMA-BUF"); if(handles[1]!=handles[0]) b.handles.push_back(handles[1]); }
        check(drmModeAddFB2(fd,3840,2160,DRM_FORMAT_NV12,handles,pitches,offsets,&b.id,0)==0,"create NV12 framebuffer");
        fbs.emplace(key,b); return b.id;
    }
    void present(Picture& p,Camera& camera) {
        uint32_t fb=framebuffer(p,camera); auto r=drmModeAtomicAlloc(); check(r,"atomic request");
        if(!active) {
            add(r,connector,DRM_MODE_OBJECT_CONNECTOR,"CRTC_ID",crtc);
            add(r,crtc,DRM_MODE_OBJECT_CRTC,"MODE_ID",mode_blob); add(r,crtc,DRM_MODE_OBJECT_CRTC,"ACTIVE",1);
        }
        for(auto kv:std::vector<std::pair<const char*,uint64_t>>{{"FB_ID",fb},{"CRTC_ID",crtc},{"SRC_X",0},{"SRC_Y",0},
            {"SRC_W",3840u<<16},{"SRC_H",2160u<<16},{"CRTC_X",0},{"CRTC_Y",0},{"CRTC_W",1920},{"CRTC_H",1080}})
            add(r,plane,DRM_MODE_OBJECT_PLANE,kv.first,kv.second);
        bool changed=overlay_dirty;
        if(overlay_plane && (changed || !active)) {
            for(auto kv:std::vector<std::pair<const char*,uint64_t>>{{"FB_ID",overlay_buffers[overlay_index].fb},{"CRTC_ID",crtc},
                {"SRC_X",0},{"SRC_Y",0},{"SRC_W",overlay_width<<16},{"SRC_H",overlay_height<<16},{"CRTC_X",16},{"CRTC_Y",16},
                {"CRTC_W",overlay_width},{"CRTC_H",overlay_height},{"zpos",10},{"alpha",65535},{"pixel blend mode",0}})
                add(r,overlay_plane,DRM_MODE_OBJECT_PLANE,kv.first,kv.second);
        }
        p.trace->display_in=now_us(); flip=Flip{};
        uint32_t flags=DRM_MODE_ATOMIC_NONBLOCK|DRM_MODE_PAGE_FLIP_EVENT;
        if(!active) flags|=DRM_MODE_ATOMIC_ALLOW_MODESET;
        int rc=drmModeAtomicCommit(fd,r,flags,&flip); drmModeAtomicFree(r); check(rc==0,"DRM atomic commit"); active=true;
        drmEventContext ev{}; ev.version=2; ev.page_flip_handler=flipped;
        int64_t deadline=now_us()+1500000;
        while(!flip.done && now_us()<deadline) {
            pollfd poller{fd,POLLIN,0}; int result=poll(&poller,1,100);
            if(result<0 && errno==EINTR) continue;
            check(result>=0,"DRM poll"); if(result>0) check(drmHandleEvent(fd,&ev)==0,"DRM flip event");
        }
        check(flip.done,"HDMI flip timeout / disconnected");
        check(flip.event>=p.trace->dq && flip.event<=flip.received+1000,"invalid flip timestamp clock");
        p.trace->flip_event=flip.event; p.trace->flip_received=flip.received;
        overlay_dirty=false;
    }
    void disable() {
        if(fd<0 || !active) return;
        lifecycle("DRM_DISABLE_BEGIN");
        auto r=drmModeAtomicAlloc();
        if(r) { try { add(r,plane,DRM_MODE_OBJECT_PLANE,"FB_ID",0); add(r,plane,DRM_MODE_OBJECT_PLANE,"CRTC_ID",0);
                if(overlay_plane) {add(r,overlay_plane,DRM_MODE_OBJECT_PLANE,"FB_ID",0);add(r,overlay_plane,DRM_MODE_OBJECT_PLANE,"CRTC_ID",0);}
                add(r,connector,DRM_MODE_OBJECT_CONNECTOR,"CRTC_ID",0); add(r,crtc,DRM_MODE_OBJECT_CRTC,"ACTIVE",0);
                add(r,crtc,DRM_MODE_OBJECT_CRTC,"MODE_ID",0);
                if(drmModeAtomicCommit(fd,r,DRM_MODE_ATOMIC_ALLOW_MODESET,nullptr)!=0) std::cerr<<"DRM disable failed: "<<strerror(errno)<<'\n';
            } catch(const std::exception& e) { std::cerr<<e.what()<<'\n'; }
            drmModeAtomicFree(r); }
        active=false;
        lifecycle("DRM_DISABLE_DONE");
    }
    ~Display() {
        disable();
        if(fd<0) return;
        lifecycle("DRM_BUFFERS_CLOSE_BEGIN");
        for(auto& b:overlay_buffers) {
            if(b.fb) drmModeRmFB(fd,b.fb);
            if(b.data!=MAP_FAILED) munmap(b.data,b.size);
            if(b.handle) {drm_mode_destroy_dumb destroy{};destroy.handle=b.handle;ioctl(fd,DRM_IOCTL_MODE_DESTROY_DUMB,&destroy);}
        }
        std::vector<uint32_t> handles;
        for(auto& kv:fbs) { drmModeRmFB(fd,kv.second.id); handles.insert(handles.end(),kv.second.handles.begin(),kv.second.handles.end()); }
        std::sort(handles.begin(),handles.end()); handles.erase(std::unique(handles.begin(),handles.end()),handles.end());
        for(auto h:handles) { drm_gem_close c{}; c.handle=h; ioctl(fd,DRM_IOCTL_GEM_CLOSE,&c); }
        if(mode_blob) drmModeDestroyPropertyBlob(fd,mode_blob);
        drmDropMaster(fd); close(fd);
        lifecycle("DRM_BUFFERS_CLOSE_DONE");
    }
};

int main(int argc,char** argv) {
    try {
        auto o=options(argc,argv); std::signal(SIGINT,[](int){interrupted=1;}); std::signal(SIGTERM,[](int){interrupted=1;});
        lifecycle_path=o.out+"/lifecycle.log";lifecycle("RUN_START");
        int64_t start=now_us(); Records records(o,start); Counters c;
        {std::ofstream timing(o.out+"/timing.json");timing<<"{\"start_monotonic_us\":"<<start<<",\"warmup_seconds\":"<<o.warmup<<"}\n";}
        Camera camera; Codec codec; Display display;
        display.open(o); if(o.codec!="direct") codec.open(o); camera.open(o);
        Latest<Raw> raw_queue; Latest<Picture> display_queue;
        // Main retains the actual scanout frame until all workers stop and
        // the display is disabled. Error exits use the same ownership order.
        std::shared_ptr<Picture> scanout_picture;
        std::mutex pending_mu,error_mu,encoding_mu;
        std::condition_variable encoding_cv;
        std::map<int64_t,TracePtr> pending;
        std::map<int64_t,std::pair<TracePtr,int>> encoding;
        std::deque<int> free_inputs{0,1};
        std::string error; std::atomic<bool> running{true},submit_done{false}; std::atomic<int64_t> quality_id{0};
        std::vector<std::thread> threads;
        auto worker=[&](auto function) { threads.emplace_back([&,function] {
            try { function(); } catch(const std::exception& e) {
                { std::lock_guard<std::mutex> lock(error_mu); if(error.empty()) error=e.what(); }
                ++c.errors; running=false;
            }
        }); };
        camera.start();
        worker([&] {
            uint32_t last=0; bool have=false;
            while(running && !interrupted) {
                auto r=camera.take(); if(!r) continue;
                r->trace->id=++c.captured;
                if(have && uint32_t(r->trace->sequence-last)>1) {
                    auto gap=uint32_t(r->trace->sequence-last)-1;
                    if(r->trace->dq-start>=int64_t(o.warmup)*1000000) c.gaps+=gap;
                    else c.startup_gaps+=gap;
                }
                last=r->trace->sequence;have=true;
                if(r->trace->flags & V4L2_BUF_FLAG_ERROR) ++c.errors;
                auto old=raw_queue.put(r); if(old) { ++c.raw_drops; records.record(old->trace,"raw_replaced"); }
            }
        });
        worker([&] {
            bool injected=false;
            while(running && !interrupted) {
                if(!injected && o.stall_ms>0 && now_us()-start>int64_t(o.stall_after)*1000000) {
                    injected=true; std::this_thread::sleep_for(std::chrono::milliseconds(o.stall_ms)); }
                int input_index=-1;
                if(o.codec!="direct") {
                    std::unique_lock<std::mutex> lock(encoding_mu);
                    encoding_cv.wait_for(lock,std::chrono::milliseconds(100),[&]{return !free_inputs.empty() || !running;});
                    if(!running) break;
                    if(free_inputs.empty()) continue;
                    input_index=free_inputs.front(); free_inputs.pop_front();
                }
                auto return_input=[&] {
                    if(input_index>=0) {std::lock_guard<std::mutex> lock(encoding_mu);free_inputs.push_back(input_index);encoding_cv.notify_all();}
                };
                auto raw=raw_queue.get(); if(!raw) {return_input();continue;} auto t=raw->trace;
                if((t->flags&V4L2_BUF_FLAG_TIMESTAMP_MASK)==V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC &&
                   (t->driver>t->dq || t->dq-t->driver>1000000)) {
                    ++c.stale_drops;records.record(t,"invalid_driver_timestamp");return_input();continue;
                }
                if(now_us()-t->dq>int64_t(o.max_age_ms)*1000) { ++c.stale_drops; records.record(t,"stale_raw"); return_input(); continue; }
                if(o.codec=="direct") {
                    auto p=std::make_shared<Picture>();p->raw=raw;p->trace=t;
                    auto old=display_queue.put(p); if(old) { ++c.display_drops; records.record(old->trace,"display_replaced"); } continue;
                }
                auto input=codec.inputs[input_index];
                t->copy_start=now_us(); codec.copy(camera,*raw,input); t->copy_end=now_us(); raw.reset();
                // Save a matching original/decoded frame during warmup only.
                if(!quality_id && t->dq-start>1000000 && t->dq-start<int64_t(o.warmup)*1000000) {
                    quality_id=t->id;
                    std::ofstream source(o.out+"/quality-source.nv12",std::ios::binary);
                    mcheck(mpp_buffer_sync_ro_begin(input),"quality source sync");
                    source.write(static_cast<const char*>(mpp_buffer_get_ptr(input)),size_t(codec.hs)*codec.vs*3/2);
                    mcheck(mpp_buffer_sync_ro_end(input),"quality source sync end");
                }
                MppFrame frame=codec.frames[input_index];
                mpp_frame_set_pts(frame,t->id); t->enc_in=now_us();
                {std::lock_guard<std::mutex> lock(encoding_mu);encoding[t->id]={t,input_index};}
                encoding_cv.notify_all();
                int64_t deadline=now_us()+1000000; MPP_RET ret;
                do {ret=codec.ea->encode_put_frame(codec.enc,frame);if(ret!=MPP_OK) std::this_thread::sleep_for(std::chrono::milliseconds(1));}
                while(ret!=MPP_OK && running && now_us()<deadline);
                mcheck(ret,"encode_put_frame");
            }
        });
        if(o.codec!="direct") worker([&] {
            std::ofstream encoded_sample(o.out+(o.codec=="hevc"?"/sample.hevc":"/sample.h264"),std::ios::binary);
            while(true) {
                {std::unique_lock<std::mutex> lock(encoding_mu);
                    encoding_cv.wait_for(lock,std::chrono::milliseconds(100),[&]{return !encoding.empty() || submit_done;});
                    if(submit_done && encoding.empty()) break;
                    if(encoding.empty()) continue;}
                std::vector<uint8_t> au; bool done=false;
                int64_t pts=-1;
                while(!done) {
                    MppPacket packet=nullptr; mcheck(codec.ea->encode_get_packet(codec.enc,&packet),"encode_get_packet"); check(packet,"empty encoder packet");
                    pts=mpp_packet_get_pts(packet);
                    size_t n=mpp_packet_get_length(packet); auto data=static_cast<const uint8_t*>(mpp_packet_get_pos(packet));
                    au.insert(au.end(),data,data+n); done=!mpp_packet_is_partition(packet)||mpp_packet_is_eoi(packet);mpp_packet_deinit(&packet);
                }
                TracePtr t;
                {std::lock_guard<std::mutex> lock(encoding_mu);auto i=encoding.find(pts);
                    check(i!=encoding.end(),"encoder PTS/frame ID mismatch");t=i->second.first;
                    free_inputs.push_back(i->second.second);encoding.erase(i);}
                encoding_cv.notify_all();
                t->enc_out=now_us();++c.encoded;c.bytes+=au.size();
                if(t->dq-start<6000000) encoded_sample.write(reinterpret_cast<const char*>(au.data()),au.size());
                { std::lock_guard<std::mutex> lock(pending_mu); check(pending.size()<120,"decoder backlog exceeds two seconds"); pending[t->id]=t; }
                MppPacket packet=nullptr; mcheck(mpp_packet_init(&packet,au.data(),au.size()),"decoder AU");mpp_packet_set_pts(packet,t->id);
                t->dec_in=now_us(); int64_t deadline=now_us()+1000000;
                MPP_RET ret;
                do { ret=codec.da->decode_put_packet(codec.dec,packet); if(ret!=MPP_OK) std::this_thread::sleep_for(std::chrono::milliseconds(1)); }
                while(ret!=MPP_OK && running && now_us()<deadline);
                mpp_packet_deinit(&packet);mcheck(ret,"decode_put_packet deadline");
            }
        });
        if(o.codec!="direct") worker([&] {
            while(running && !interrupted) {
                MppFrame frame=nullptr; mcheck(codec.da->decode_get_frame(codec.dec,&frame),"decode_get_frame");
                if(!frame) { std::this_thread::sleep_for(std::chrono::microseconds(300)); continue; }
                if(mpp_frame_get_info_change(frame)) {
                    std::cerr<<"DECODER_INFO "<<mpp_frame_get_width(frame)<<'x'<<mpp_frame_get_height(frame)<<" stride="
                        <<mpp_frame_get_hor_stride(frame)<<'x'<<mpp_frame_get_ver_stride(frame)<<'\n';
                    auto ret=codec.da->control(codec.dec,MPP_DEC_SET_INFO_CHANGE_READY,nullptr);mpp_frame_deinit(&frame);mcheck(ret,"decoder info ready");continue;
                }
                if(mpp_frame_get_errinfo(frame)||mpp_frame_get_discard(frame)) { mpp_frame_deinit(&frame);throw std::runtime_error("MPP decode error/discard"); }
                int64_t pts=mpp_frame_get_pts(frame); TracePtr t;
                { std::lock_guard<std::mutex> lock(pending_mu); auto i=pending.find(pts); if(i!=pending.end()) {t=i->second;pending.erase(i);} }
                if(!t) {mpp_frame_deinit(&frame);throw std::runtime_error("decoder PTS/frame ID mismatch: "+std::to_string(pts));}
                t->dec_out=now_us();++c.decoded;
                if(pts==quality_id) {
                    auto buffer=mpp_frame_get_buffer(frame);
                    mcheck(mpp_buffer_sync_ro_begin(buffer),"quality decoded sync");
                    auto data=static_cast<const char*>(mpp_buffer_get_ptr(buffer));
                    unsigned pitch=mpp_frame_get_hor_stride(frame),vertical=mpp_frame_get_ver_stride(frame);
                    std::ofstream decoded(o.out+"/quality-decoded.nv12",std::ios::binary);
                    for(unsigned y=0;y<2160;++y) decoded.write(data+size_t(y)*pitch,3840);
                    for(unsigned y=0;y<1080;++y) decoded.write(data+size_t(pitch)*vertical+size_t(y)*pitch,3840);
                    mcheck(mpp_buffer_sync_ro_end(buffer),"quality decoded sync end");
                }
                auto p=std::make_shared<Picture>();p->frame=frame;p->trace=t;
                auto old=display_queue.put(p);if(old) {++c.display_drops;records.record(old->trace,"display_replaced");}
            }
        });
        worker([&] {
            try {
                while(running && !interrupted) {
                    int64_t prepare_start=now_us();
                    if(display.prepare_overlay()) {
                        int64_t cost=now_us()-prepare_start;
                        if(cost>c.max_overlay_prepare_us) c.max_overlay_prepare_us=cost;
                    }
                    int64_t target=o.display_scheduling=="deadline" ? display.next_target_us() : 0;
                    auto p=target ? display_queue.get_until(target-o.display_margin_us) : display_queue.get();
                    if(!p) continue;
                    p->trace->display_target=target;
                    if(o.display_error_after && now_us()-start>int64_t(o.display_error_after)*1000000) {
                        lifecycle("INJECT_DISPLAY_ERROR");
                        records.record(p->trace,"injected_display_error");
                        throw std::runtime_error("injected HDMI display failure (not physical unplug)");
                    }
                    if(now_us()-p->trace->dq>int64_t(o.max_age_ms)*1000) {++c.stale_drops;records.record(p->trace,"stale_display");continue;}
                    display.present(*p,camera);++c.displayed;c.last_latency_us=p->trace->flip_event-p->trace->dq;
                    if(target && p->trace->dq-start>=int64_t(o.warmup)*1000000 &&
                        p->trace->flip_event>target+display.period_us()/2) ++c.missed_targets;
                    records.record(p->trace,"displayed");scanout_picture=p;
                }
            } catch(...) {running=false;lifecycle("DISPLAY_ERROR_STOP_REQUESTED");throw;}
        });
        std::ofstream rates(o.out+"/rates.csv");rates<<"elapsed_s,captured,encoded,decoded,displayed,bytes,raw_drops,display_drops,stale_drops,gaps,errors,last_latency_ms\n";
        int64_t last_progress=start;uint64_t last_count=0;
        while(running && !interrupted && (!o.seconds || now_us()-start<int64_t(o.seconds+o.warmup)*1000000)) {
            double elapsed=(now_us()-start)/1000000.0;
            rates<<elapsed<<','<<c.captured<<','<<c.encoded<<','<<c.decoded<<','<<c.displayed<<','<<c.bytes<<','<<c.raw_drops<<','<<c.display_drops
                <<','<<c.stale_drops<<','<<c.gaps<<','<<c.errors<<','<<c.last_latency_us/1000.0<<'\n';rates.flush();
            {std::ofstream status(o.out+"/status.json.tmp");status<<"{\"running\":true,\"elapsed_s\":"<<elapsed<<",\"captured\":"<<c.captured<<",\"encoded\":"<<c.encoded
                <<",\"decoded\":"<<c.decoded<<",\"displayed\":"<<c.displayed<<",\"last_latency_ms\":"<<c.last_latency_us/1000.0
                <<",\"raw_drops\":"<<c.raw_drops<<",\"display_drops\":"<<c.display_drops<<",\"stale_drops\":"<<c.stale_drops
                <<",\"missed_targets\":"<<c.missed_targets<<",\"errors\":"<<c.errors<<"}\n";}
            rename((o.out+"/status.json.tmp").c_str(),(o.out+"/status.json").c_str());
            if(c.displayed!=last_count) {last_count=c.displayed;last_progress=now_us();}
            if(now_us()-last_progress>10000000) {std::lock_guard<std::mutex> lock(error_mu);error="no displayed frame for 10 seconds";running=false;}
            std::this_thread::sleep_for(std::chrono::milliseconds(1000));
        }
        running=false;
        lifecycle("WORKERS_JOIN_BEGIN");
        threads[0].join();threads[1].join();submit_done=true;encoding_cv.notify_all();
        for(size_t i=2;i<threads.size();++i) threads[i].join();
        lifecycle("WORKERS_JOIN_DONE");
        display.disable();
        scanout_picture.reset();
        auto raw=raw_queue.drain();if(raw) records.record(raw->trace,"stopped_raw");raw.reset();
        auto picture=display_queue.drain();if(picture) records.record(picture->trace,"stopped_display");picture.reset();
        for(auto& kv:pending) records.record(kv.second,"stopped_decoder");
        pending.clear();
        for(auto& kv:encoding) records.record(kv.second.first,"stopped_encoder");
        encoding.clear();
        std::ofstream summary(o.out+"/summary.json");
        summary<<"{\"codec\":\""<<o.codec<<"\",\"display_scheduling\":\""<<o.display_scheduling
            <<"\",\"display_margin_us\":"<<o.display_margin_us<<",\"missed_targets\":"<<c.missed_targets
            <<",\"max_overlay_prepare_ms\":"<<c.max_overlay_prepare_us/1000.0
            <<",\"capture_buffers_actual\":"<<camera.count<<",\"elapsed_s\":"<<(now_us()-start)/1000000.0
            <<",\"captured\":"<<c.captured<<",\"encoded\":"<<c.encoded<<",\"decoded\":"<<c.decoded<<",\"displayed\":"<<c.displayed<<",\"bytes\":"<<c.bytes
            <<",\"raw_drops\":"<<c.raw_drops<<",\"display_drops\":"<<c.display_drops<<",\"stale_drops\":"<<c.stale_drops<<",\"driver_gaps\":"<<c.gaps
            <<",\"startup_sequence_gaps\":"<<c.startup_gaps<<",\"errors\":"<<c.errors<<",\"interrupted\":"<<(interrupted?"true":"false")<<",\"latency\":";
        records.summary(summary);summary<<"}\n";
        std::ofstream status(o.out+"/status.json");status<<"{\"running\":false,\"error_present\":"<<(!error.empty()?"true":"false")<<"}\n";
        if(!error.empty()) {std::ofstream err(o.out+"/error.txt");err<<error<<'\n';std::cerr<<error<<'\n';lifecycle("RUN_ERROR_EXIT");return 1;}
        lifecycle("RUN_NORMAL_EXIT");return 0;
    } catch(const std::exception& e) {std::cerr<<"FATAL: "<<e.what()<<'\n';return 1;}
}
