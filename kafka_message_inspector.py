from kafka import KafkaConsumer
import json
from datetime import datetime

# KAFKA MESSAGE INSPECTOR
# Shows the raw structure of messages in the video_clips topic

KAFKA_BOOTSTRAP_SERVERS = ['localhost:9094']
KAFKA_TOPIC = 'video_clips'

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

log("Connecting to Kafka...")
consumer = KafkaConsumer(
    KAFKA_TOPIC,
    bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
    auto_offset_reset='latest',  # Only show new messages
    group_id='inspector-group'
)

log(f"Listening to topic '{KAFKA_TOPIC}'...")
log("Waiting for messages (run step5_sync_producer.py in another terminal)...\n")

message_count = 0
for message in consumer:
    message_count += 1
    
    print(f"\n{'='*80}")
    print(f"MESSAGE #{message_count}")
    print(f"{'='*80}")
    
    # Metadata
    print(f"Partition: {message.partition}")
    print(f"Offset:    {message.offset}")
    print(f"Timestamp: {datetime.fromtimestamp(message.timestamp/1000).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
    
    # Headers
    print(f"\nHeaders:")
    if message.headers:
        for key, value in message.headers:
            decoded_value = value.decode('utf-8')
            print(f"  {key}: {decoded_value}")
    else:
        print("  (none)")
    
    # Payload
    print(f"\nPayload:")
    print(f"  Type: PNG image (binary, lossless)")
    print(f"  Size: {len(message.value):,} bytes")
    
    # Show first few bytes (PNG signature)
    if len(message.value) >= 10:
        hex_preview = ' '.join(f'{b:02x}' for b in message.value[:10])
        print(f"  First 10 bytes: {hex_preview}")
        if message.value[0:8] == b'\x89PNG\r\n\x1a\n':
            print(f"  ✓ Valid PNG signature detected")
        elif message.value[0:2] == b'\xff\xd8':
            print(f"  ⚠ JPEG detected (should be PNG)")
    
    print(f"\n{'='*80}\n")
    
    # Stop after 5 messages
    if message_count >= 5:
        log("Showed 5 messages. Exiting.")
        break

consumer.close()
