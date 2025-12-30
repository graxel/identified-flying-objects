import torch
import torchvision.transforms as T
from torchvision.models import resnet18, ResNet18_Weights
from PIL import Image
import numpy as np
from typing import List, Tuple

class ObjectClassifier:
    def __init__(self, device: str = "cpu"):
        self.device = device
        # Load pretrained ResNet18
        self.model = resnet18(weights=ResNet18_Weights.DEFAULT)
        self.model.eval()
        self.model.to(device)
        
        # Preprocessing transforms
        self.transforms = T.Compose([
            T.Resize((224, 224)),
            T.ToTensor(),
            T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        ])
        
        # Placeholder class mapping (ImageNet classes are not ideal for "drone vs bird", 
        # but we start with pretrained and will fine-tune later)
        # For now, we just output the raw ImageNet class or a dummy mapping.
        # In a real scenario, we'd replace the final layer.
        
    def predict(self, crops: List[np.ndarray]) -> List[Tuple[str, float]]:
        """
        Classifies a list of image crops.
        Returns list of (class_label, confidence).
        """
        if not crops:
            return []
            
        # Convert numpy crops (BGR) to PIL Images (RGB)
        pil_images = [Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)) for crop in crops]
        
        # Batch process
        batch = torch.stack([self.transforms(img) for img in pil_images]).to(self.device)
        
        with torch.no_grad():
            outputs = self.model(batch)
            probs = torch.nn.functional.softmax(outputs, dim=1)
            
        results = []
        for prob in probs:
            top_prob, top_catid = torch.topk(prob, 1)
            # Just return the ID for now as we don't have the label map loaded yet
            results.append((str(top_catid.item()), top_prob.item()))
            
        return results
