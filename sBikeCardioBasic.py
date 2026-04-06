import asyncio
import threading
import json
import time
import os
import sys
import customtkinter as ctk
from bleak import BleakScanner, BleakClient
from datetime import datetime

# --- CONFIG ---
UUID_CADENCE = "00002a5b-0000-1000-8000-00805f9b34fb"
UUID_SPEED   = "00002a5c-0000-1000-8000-00805f9b34fb"
CONFIG_FILE  = "cycling_config.json"
LOCK_FILE    = "app_cycling.lock"

class RockBrosMuot(ctk.CTk):
    def __init__(self):
        # 1. KIỂM TRA APP ĐANG CHẠY
        self.check_single_instance()
        
        super().__init__()
        self.title("sBike Cardio v4.6 - by Liêm Phan")
        self.geometry("900x880") # Tăng nhẹ chiều cao để chứa thêm dòng tác giả
        ctk.set_appearance_mode("dark")

        # Logic States
        self.client = None
        self.is_connected = False
        self.stop_threads = False
        self.found_devices = {}
        self.last_rev = None
        self.last_time = None
        self.session_speeds = []
        self.start_time = None
        
        # Logic States cho Quãng đường & Auto-Pause Timer
        self.active_session_time = 0   # Thời gian ĐẠP THỰC TẾ (giây)
        self.session_distance = 0.0    # Quãng đường đi được lần này (KM)
        self.last_pedal_time = None    # Mốc thời gian cuối cùng có tín hiệu đạp
        
        self.config = self.load_config()
        self.setup_ui()

        # 2. XỬ LÝ ĐÓNG APP SẠCH SẼ
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        # 3. KÍCH HOẠT TỰ ĐỘNG KẾT NỐI
        if self.config.get("last_mac"):
            threading.Thread(target=self.auto_hunter_loop, daemon=True).start()
            
        self.update_timer_ui()

    def check_single_instance(self):
        if os.path.exists(LOCK_FILE):
            try:
                os.remove(LOCK_FILE)
            except:
                print("⚠️ App đã đang chạy!")
                sys.exit()
        with open(LOCK_FILE, "w") as f: f.write("running")

    def load_config(self):
        default = {"totals": {"km": 0.0, "total_time": 0, "sessions": 0}, "last_mac": None, "last_name": None}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if "total_time" not in data["totals"]: data["totals"]["total_time"] = 0
                    if "sessions" not in data["totals"]: data["totals"]["sessions"] = 0
                    return data
            except: pass
        return default

    def add_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.txt_logs.configure(state="normal")
        self.txt_logs.insert("end", f"[{timestamp}] {message}\n")
        self.txt_logs.see("end")
        self.txt_logs.configure(state="disabled")

    def format_time(self, seconds):
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def setup_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1) # Cho phép dash chính co giãn

        self.sidebar = ctk.CTkFrame(self, width=280)
        self.sidebar.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        
        ctk.CTkLabel(self.sidebar, text="sBIKE CARDIO", font=("Arial", 22, "bold")).pack(pady=20)
        self.lbl_status = ctk.CTkLabel(self.sidebar, text="🔍 Chế độ chờ...", text_color="gray")
        self.lbl_status.pack(pady=5)

        self.btn_scan = ctk.CTkButton(self.sidebar, text="QUÉT MỚI", command=self.manual_scan)
        self.btn_scan.pack(pady=10, padx=20)
        
        self.device_combo = ctk.CTkOptionMenu(self.sidebar, values=["Chọn thiết bị..."])
        self.device_combo.pack(pady=5, padx=20)

        self.btn_conn = ctk.CTkButton(self.sidebar, text="KẾT NỐI", fg_color="#27ae60", command=self.manual_connect)
        self.btn_conn.pack(pady=10, padx=20)

        ctk.CTkLabel(self.sidebar, text="--- THỐNG KÊ TÍCH LŨY ---", font=("Arial", 12, "bold")).pack(pady=(30, 5))
        self.lbl_total_time = ctk.CTkLabel(self.sidebar, text=f"Thời gian: {self.format_time(self.config['totals']['total_time'])}")
        self.lbl_total_time.pack()
        self.lbl_total_sessions = ctk.CTkLabel(self.sidebar, text=f"Số lần tập: {self.config['totals']['sessions']}")
        self.lbl_total_sessions.pack()

        # Dash chính
        self.dash = ctk.CTkFrame(self, fg_color="transparent")
        self.dash.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        
        # 3 Ô dữ liệu chính
        self.val_speed = self.create_card(self.dash, "TỐC ĐỘ (KM/H)", "0.0", "#f1c40f")
        self.val_rpm = self.create_card(self.dash, "NHỊP ĐẠP (RPM)", "0", "#3498db")
        self.val_distance = self.create_card(self.dash, "QUÃNG ĐƯỜNG ĐANG ĐI (KM)", "0.00", "#e67e22")
        
        # Panel hiển thị Thời gian tập lần này
        self.session_info = ctk.CTkFrame(self.dash, fg_color="#2c3e50")
        self.session_info.pack(fill="x", pady=10)
        
        self.lbl_session_time = ctk.CTkLabel(self.session_info, text="THỜI GIAN LẦN TẬP LUYỆN NÀY: 00:00:00", font=("Arial", 16, "bold"), text_color="#2ecc71")
        self.lbl_session_time.pack(pady=5)

        # Hiển thị tổng quãng đường tích lũy (Cập nhật real-time)
        self.lbl_total = ctk.CTkLabel(self.dash, text=f"TỔNG QUÃNG ĐƯỜNG ĐÃ TÍCH LŨY: {self.config['totals']['km']:.2f} KM", font=("Arial", 18, "bold"))
        self.lbl_total.pack(pady=10)

        # Khung Logs
        self.txt_logs = ctk.CTkTextbox(self.dash, height=130, font=("Consolas", 12), state="disabled")
        self.txt_logs.pack(fill="x", pady=10)

        # --- BỔ SUNG GIAO DIỆN TÁC GIẢ Ở MÉP DƯỚI ---
        self.footer = ctk.CTkFrame(self, height=30, fg_color="transparent")
        self.footer.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.lbl_author = ctk.CTkLabel(self.footer, 
                                     text="Tên tác giả Liêm Phan - facebook: fb.com/phanvanliem", 
                                     font=("Arial", 11), 
                                     text_color="gray")
        self.lbl_author.pack(side="right", padx=20, pady=5)
        
        self.add_log("Ứng dụng đã khởi động.")

    def create_card(self, master, title, val, color):
        f = ctk.CTkFrame(master, corner_radius=15, border_width=1, border_color="#34495e")
        f.pack(fill="x", pady=10)
        ctk.CTkLabel(f, text=title, font=("Arial", 12, "bold")).pack(pady=(10,0))
        lbl = ctk.CTkLabel(f, text=val, font=("Arial", 90, "bold"), text_color=color)
        lbl.pack(pady=10)
        return lbl

    def update_timer_ui(self):
        if self.is_connected and self.start_time:
            if self.last_pedal_time and (time.time() - self.last_pedal_time < 2.5):
                self.active_session_time += 1
            self.lbl_session_time.configure(text=f"THỜI GIAN LẦN TẬP LUYỆN NÀY: {self.format_time(self.active_session_time)}")
        else:
            self.lbl_session_time.configure(text="THỜI GIAN LẦN TẬP LUYỆN NÀY: 00:00:00")
        self.after(1000, self.update_timer_ui)

    def auto_hunter_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while not self.is_connected and not self.stop_threads:
            mac = self.config.get("last_mac")
            if mac:
                name = self.config.get('last_name')
                self.lbl_status.configure(text=f"🔄 Đang tìm: {name}", text_color="#f39c12")
                try:
                    dev = loop.run_until_complete(BleakScanner.find_device_by_address(mac, timeout=4.0))
                    if dev:
                        self.add_log(f"Tìm thấy thiết bị cũ: {name}. Đang kết nối...")
                        loop.run_until_complete(self.connect_logic(dev))
                except: pass
            time.sleep(5)

    async def connect_logic(self, device):
        if self.is_connected: return
        try:
            async with BleakClient(device, timeout=10.0) as client:
                self.client = client
                self.is_connected = True
                self.config["last_mac"], self.config["last_name"] = device.address, device.name
                
                self.add_log(f"Kết nối thành công tới {device.name}!")
                self.lbl_status.configure(text="✅ ONLINE", text_color="#2ecc71")
                self.btn_conn.configure(text="NGẮT", fg_color="#e74c3c")
                await asyncio.sleep(1.5)
                
                target_uuid = None
                for s in client.services:
                    for char in s.characteristics:
                        u = char.uuid.lower()
                        if UUID_CADENCE in u or UUID_SPEED in u:
                            target_uuid = char.uuid
                            self.mode = 'cadence' if UUID_CADENCE in u else 'speed'
                            break

                self.add_log(f"Chế độ cảm biến: {self.mode.upper()}")
                
                # Reset Session Data
                self.start_time = time.time()
                self.session_speeds = []
                self.last_rev = None
                self.active_session_time = 0
                self.session_distance = 0.0
                self.last_pedal_time = None
                self.val_distance.configure(text="0.00")

                try:
                    await client.start_notify(target_uuid, self.data_handler)
                    while self.is_connected and client.is_connected:
                        await asyncio.sleep(1)
                    await client.stop_notify(target_uuid)
                except Exception as e:
                    self.add_log(f"Lỗi Notify: {e}. Chuyển sang Polling...")
                    while self.is_connected and client.is_connected:
                        try:
                            data = await client.read_gatt_char(target_uuid)
                            self.data_handler(None, data)
                        except: pass
                        await asyncio.sleep(0.5)
        except Exception as e:
            self.add_log(f"Lỗi kết nối: {e}")
        finally:
            self.is_connected = False
            self.save_and_reset()

    def data_handler(self, sender, data):
        try:
            rev, evt = int.from_bytes(data[1:3], 'little'), int.from_bytes(data[3:5], 'little')
            if self.last_rev is not None:
                d_rev, d_evt = (rev - self.last_rev) & 0xFFFF, (evt - self.last_time) & 0xFFFF
                
                if 0 < d_evt < 65535:
                    rpm = (d_rev * 1024 * 60) / d_evt
                    speed = (rpm * 2.5 * 2.1 * 60) / 1000
                    
                    if rpm > 1:
                        self.last_pedal_time = time.time()
                        self.session_speeds.append(speed)
                        
                        # Tính quãng đường: Số vòng crank x Tỷ số truyền (2.5) x Chu vi bánh (2.1)
                        delta_dist_km = (d_rev * 2.5 * 2.1) / 1000
                        self.session_distance += delta_dist_km
                        
                        # Cập nhật UI
                        self.val_rpm.configure(text=str(int(rpm)) if self.mode=='cadence' else "0")
                        self.val_speed.configure(text=f"{speed:.1f}")
                        self.val_distance.configure(text=f"{self.session_distance:.2f}")
                        
                        # ĐIỀU CHỈNH: Hiển thị tổng quãng đường tích lũy cộng dồn real-time
                        current_total = self.config["totals"]["km"] + self.session_distance
                        self.lbl_total.configure(text=f"TỔNG QUÃNG ĐƯỜNG ĐÃ TÍCH LŨY: {current_total:.2f} KM")
                        
            self.last_rev, self.last_time = rev, evt
        except: pass

    def manual_scan(self):
        self.add_log("Bắt đầu quét thiết bị xung quanh...")
        self.btn_scan.configure(text="Đang quét...", state="disabled")
        threading.Thread(target=lambda: asyncio.run(self.do_scan()), daemon=True).start()

    async def do_scan(self):
        devices = await BleakScanner.discover(timeout=5.0)
        names = []
        for d in devices:
            if d.name and "ROCKBROS" in d.name.upper():
                key = f"{d.name} ({d.address})"
                self.found_devices[key] = d
                names.append(key)
        
        self.device_combo.configure(values=names if names else ["Không tìm thấy"])
        if names: 
            self.device_combo.set(names[0])
            self.add_log(f"Tìm thấy {len(names)} thiết bị.")
        else:
            self.add_log("Không tìm thấy thiết bị cảm biến nào.")
        self.btn_scan.configure(text="QUÉT MỚI", state="normal")

    def manual_connect(self):
        if not self.is_connected:
            sel = self.device_combo.get()
            if sel in self.found_devices:
                self.add_log(f"Đang kết nối thủ công tới {sel}...")
                threading.Thread(target=lambda: asyncio.run(self.connect_logic(self.found_devices[sel])), daemon=True).start()
        else: 
            self.add_log("Đang ngắt kết nối chủ động...")
            self.is_connected = False

    def save_and_reset(self):
        if self.start_time:
            duration = self.active_session_time
            if duration > 5:
                dist = self.session_distance
                self.config["totals"]["km"] += dist
                self.config["totals"]["total_time"] += duration
                self.config["totals"]["sessions"] += 1
                with open(CONFIG_FILE, 'w') as f: json.dump(self.config, f, indent=4)
                
                self.lbl_total.configure(text=f"TỔNG QUÃNG ĐƯỜNG ĐÃ TÍCH LŨY: {self.config['totals']['km']:.2f} KM")
                self.lbl_total_time.configure(text=f"Thời gian: {self.format_time(self.config['totals']['total_time'])}")
                self.lbl_total_sessions.configure(text=f"Số lần tập: {self.config['totals']['sessions']}")
                self.add_log(f"Đã lưu phiên tập: {dist:.2f}km trong {self.format_time(duration)}")

        self.val_rpm.configure(text="0"); self.val_speed.configure(text="0.0"); self.val_distance.configure(text="0.00")
        self.btn_conn.configure(text="KẾT NỐI", fg_color="#27ae60")
        self.lbl_status.configure(text="🔍 Đang chờ...", text_color="gray")
        self.add_log("Đã ngắt kết nối.")
        self.start_time = None

    def on_closing(self):
        self.stop_threads = True
        self.is_connected = False
        if os.path.exists(LOCK_FILE):
            try: os.remove(LOCK_FILE)
            except: pass
        self.destroy()
        sys.exit()

if __name__ == "__main__":
    app = RockBrosMuot()
    app.mainloop()