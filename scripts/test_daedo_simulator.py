import socket
import time
import sys

TARGET_IP = "127.0.0.1"
TARGET_PORT = 5000  # Match your plugin schema configuration

def send_packet(sock, payload: str):
    print(f"Blasting to target: {payload}")
    sock.sendto(payload.encode('utf-8'), (TARGET_IP, TARGET_PORT))
    time.sleep(0.6) # Yield pacing execution simulator

def execute_simulation():
    print(f"--- Launching Daedo Tk-Strike Hardware Packet Simulator targeting port {TARGET_PORT} ---")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    try:
        # 1. Match Initialization sequence (Sends both parts)
        send_packet(sock, "mch;2012;Male Finals;Under 68kg;;;;;;;;;;;;30")
        send_packet(sock, "at1;Lee Dae-hoon;KOR;KOR;Active;Alexey Denisenko;RUS;RUS;Active")
        
        # 2. Ready up state machine
        send_packet(sock, "rdy;1")
        send_packet(sock, "hwt;test")
        
        # 3. Round Counter assignment and Clock tick starting
        send_packet(sock, "rnd;1")
        send_packet(sock, "clk; 02:00;start")
        send_packet(sock, "clk; 01:59")
        send_packet(sock, "clk; 01:58")
        
        # 4. Point Scoring triggers
        send_packet(sock, "hl1;hit") # Blue landing structural check
        send_packet(sock, "pt1;2")   # Blue gains 2 points for a trunk kick
        send_packet(sock, "sc1;2;;0;;0") # Scoreboard reflects state
        
        send_packet(sock, "clk; 01:45")
        
        # Red lands a high head kick
        send_packet(sock, "hl2;hit")
        send_packet(sock, "pt2;3")
        send_packet(sock, "sc1;2;;3;;0")
        
        # 5. Penalties assigned
        send_packet(sock, "wg1;1;;0") # Blue picks up a Gam-jeom penalty
        
        # 6. Trigger a break state
        send_packet(sock, "brk; 00:00")
        
        # 7. Resume for round 2
        send_packet(sock, "rnd;2")
        send_packet(sock, "clk; 02:00;start")
        send_packet(sock, "clk; 01:50")
        
        # 8. Declare a winner
        send_packet(sock, "win;blue")
        
        print("--- Simulation Sequence Fully Executed cleanly ---")
    except KeyboardInterrupt:
        print("\nSimulator interrupted.")
    finally:
        sock.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        TARGET_PORT = int(sys.argv[1])
    execute_simulation()