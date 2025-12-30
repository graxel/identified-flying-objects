import streamlit as st
import asyncio
from sqlalchemy.future import select
from database.db import AsyncSessionLocal
from database.models import Detection, Track
import cv2
import numpy as np
from PIL import Image
import io

st.set_page_config(page_title="IFO Review", layout="wide")

st.title("Identified Flying Objects - Human Review")

# Sidebar for filters
st.sidebar.header("Filters")
min_conf = st.sidebar.slider("Min Confidence", 0.0, 1.0, 0.6)

async def get_uncertain_detections():
    async with AsyncSessionLocal() as session:
        # Fetch detections with low confidence or marked uncertain
        # Ideally we'd join with Track to get track info
        stmt = select(Detection).where(Detection.confidence < min_conf).limit(50)
        result = await session.execute(stmt)
        return result.scalars().all()

async def update_label(detection_id, new_label):
    async with AsyncSessionLocal() as session:
        stmt = select(Detection).where(Detection.id == detection_id)
        result = await session.execute(stmt)
        detection = result.scalar_one_or_none()
        if detection:
            detection.human_label = new_label
            detection.is_uncertain = False
            await session.commit()

# Main content
if st.button("Load Uncertain Detections"):
    detections = asyncio.run(get_uncertain_detections())
    
    if not detections:
        st.info("No uncertain detections found.")
    else:
        st.write(f"Found {len(detections)} detections to review.")
        
        cols = st.columns(3)
        for i, det in enumerate(detections):
            col = cols[i % 3]
            
            with col:
                if det.crop_image:
                    # Convert blob to image
                    image = Image.open(io.BytesIO(det.crop_image))
                    st.image(image, caption=f"ID: {det.id} | Conf: {det.confidence:.2f}")
                else:
                    st.write("No image data")
                
                st.write(f"Pred: {det.class_label}")
                
                with st.form(key=f"form_{det.id}"):
                    new_label = st.selectbox("Correct Label", ["bird", "drone", "plane", "unknown"], key=f"sel_{det.id}")
                    if st.form_submit_button("Confirm"):
                        asyncio.run(update_label(det.id, new_label))
                        st.success("Updated!")
                        st.rerun()

st.markdown("---")
st.caption("IFO System v0.1")
