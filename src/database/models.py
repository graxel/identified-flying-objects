from sqlalchemy import Column, Integer, String, Float, DateTime, LargeBinary, Boolean, ForeignKey, BigInteger
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func
import datetime

Base = declarative_base()

class Frame(Base):
    __tablename__ = 'frames'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    video_source = Column(String, nullable=True)
    
    detections = relationship("Detection", back_populates="frame")

class Track(Base):
    __tablename__ = 'tracks'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    external_track_id = Column(Integer, nullable=False) # ID from the tracker (ByteTrack/etc)
    session_id = Column(String, nullable=False) # To distinguish runs
    start_time = Column(DateTime(timezone=True), server_default=func.now())
    end_time = Column(DateTime(timezone=True), onupdate=func.now())
    predicted_class = Column(String, nullable=True)
    confirmed_class = Column(String, nullable=True)
    is_reviewed = Column(Boolean, default=False)
    
    detections = relationship("Detection", back_populates="track")

class Detection(Base):
    __tablename__ = 'detections'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    frame_id = Column(BigInteger, ForeignKey('frames.id'), nullable=False)
    track_id = Column(BigInteger, ForeignKey('tracks.id'), nullable=True)
    
    bbox_x = Column(Float, nullable=False)
    bbox_y = Column(Float, nullable=False)
    bbox_w = Column(Float, nullable=False)
    bbox_h = Column(Float, nullable=False)
    
    confidence = Column(Float, nullable=False)
    class_label = Column(String, nullable=False) # Predicted class
    
    crop_image = Column(LargeBinary, nullable=True) # BLOB for the crop
    
    human_label = Column(String, nullable=True)
    is_uncertain = Column(Boolean, default=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    frame = relationship("Frame", back_populates="detections")
    track = relationship("Track", back_populates="detections")

class HumanFeedback(Base):
    __tablename__ = 'human_feedback'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    detection_id = Column(BigInteger, ForeignKey('detections.id'), nullable=False)
    original_label = Column(String, nullable=False)
    new_label = Column(String, nullable=False)
    user_id = Column(String, nullable=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
