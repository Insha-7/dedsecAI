import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
import re
from datetime import datetime
from tqdm import tqdm
from torch.utils.data import DataLoader, TensorDataset
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"Using device: {device}")

class Autoencoder(nn.Module):
    def __init__(self, input_dim):
        super(Autoencoder, self).__init__()
        self.encoder = nn.Sequential(
            nn.Linear(input_dim, 128),
            nn.ReLU(),
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Linear(64, 32)
        )
        self.decoder = nn.Sequential(
            nn.Linear(32, 64),
            nn.ReLU(),
            nn.Linear(64, 128),
            nn.ReLU(),
            nn.Linear(128, input_dim)
        )

    def forward(self, x):
        encoded = self.encoder(x)
        decoded = self.decoder(encoded)
        return decoded


class AnomalyClassifier(nn.Module):
    def __init__(self, input_dim):
        super(AnomalyClassifier, self).__init__()
        self.model = nn.Sequential(
            nn.Linear(input_dim, 128),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(64, 1),
            nn.Sigmoid() 
        )

    def forward(self, x):
        return self.model(x)


def parse_log_line(line):
    match = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?(\bHRESULT = 0x[0-9a-fA-F]+|\bError\b|\bWarning\b)", line)
    if match:
        timestamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        error_code = match.group(2)
        return timestamp, error_code
    return None, None


def load_cbs_log(file_path, max_lines=5000000000000000000000000):
    timestamps, error_codes = [], []
    error_map, frequencies = {}, {}

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        for i, line in enumerate(tqdm(f, desc="Reading logs")):
            if i >= max_lines:
                break
            timestamp, error_code = parse_log_line(line)
            if timestamp and error_code:
                timestamps.append(timestamp.timestamp())
                if error_code not in error_map:
                    error_map[error_code] = len(error_map) + 1
                error_id = error_map[error_code]
                error_codes.append(error_id)
                frequencies[error_id] = frequencies.get(error_id, 0) + 1

    log_frequencies = np.array([frequencies[error_code] for error_code in error_codes])
    return np.array(timestamps), np.array(error_codes), log_frequencies


log_file = "cbs.log"
timestamps, error_codes, log_frequencies = load_cbs_log(log_file)

scaler = StandardScaler()
X = scaler.fit_transform(np.column_stack((timestamps, error_codes, log_frequencies)))

X_train, X_test = train_test_split(X, test_size=0.1, random_state=42)

X_train_tensor = torch.tensor(X_train, dtype=torch.float32).to(device)
X_test_tensor = torch.tensor(X_test, dtype=torch.float32).to(device)


batch_size = 8192
train_dataset = TensorDataset(X_train_tensor)
train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)


input_dim = X_train.shape[1]
autoencoder = Autoencoder(input_dim).to(device)
criterion = nn.MSELoss()
optimizer = optim.Adam(autoencoder.parameters(), lr=0.001)


num_epochs = 10
scaler_amp = torch.cuda.amp.GradScaler()

for epoch in range(num_epochs):
    autoencoder.train()
    epoch_loss = 0
    for batch in train_loader:
        optimizer.zero_grad()
        with torch.cuda.amp.autocast():
            output = autoencoder(batch[0])
            loss = criterion(output, batch[0])
        scaler_amp.scale(loss).backward()
        scaler_amp.step(optimizer)
        scaler_amp.update()
        epoch_loss += loss.item()
    print(f"Epoch {epoch+1}/{num_epochs}, Loss: {epoch_loss / len(train_loader):.6f}")

# Save trained Autoencoder
torch.save(autoencoder.state_dict(), "cbs_anomaly_detector.pth")
print("Autoencoder training complete! Model saved.")

# Anomaly Detection
autoencoder.eval()
with torch.no_grad():
    X_train_reconstructed = autoencoder(X_train_tensor).cpu().numpy()
    X_test_reconstructed = autoencoder(X_test_tensor).cpu().numpy()

# Compute Reconstruction Errors
train_errors = np.mean((X_train - X_train_reconstructed) ** 2, axis=1)
test_errors = np.mean((X_test - X_test_reconstructed) ** 2, axis=1)

# Define threshold for anomaly detection (95th percentile)
threshold = np.percentile(train_errors, 95)
y_train = (train_errors > threshold).astype(int)
y_test = (test_errors > threshold).astype(int)

# Convert Labels to PyTorch tensors
y_train_tensor = torch.tensor(y_train, dtype=torch.float32).to(device)
y_test_tensor = torch.tensor(y_test, dtype=torch.float32).to(device)

# Prepare DataLoader for Classification Model
train_dataset = TensorDataset(X_train_tensor, y_train_tensor)
train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)

# Initialize PyTorch Classifier
classifier = AnomalyClassifier(input_dim).to(device)
clf_criterion = nn.BCELoss()  # Binary Cross-Entropy Loss
clf_optimizer = optim.Adam(classifier.parameters(), lr=0.001)
lr_scheduler = optim.lr_scheduler.StepLR(clf_optimizer, step_size=5, gamma=0.5)

# Train Classifier
num_epochs = 10
for epoch in range(num_epochs):
    classifier.train()
    epoch_loss = 0
    for X_batch, y_batch in train_loader:
        clf_optimizer.zero_grad()
        y_pred = classifier(X_batch).squeeze()
        loss = clf_criterion(y_pred, y_batch)
        loss.backward()
        clf_optimizer.step()
        epoch_loss += loss.item()
    lr_scheduler.step()
    print(f"Epoch {epoch+1}/{num_epochs}, Loss: {epoch_loss / len(train_loader):.6f}")

# Save PyTorch Classifier
torch.save(classifier.state_dict(), "anomaly_classifier.pth")
print("Classifier training complete! Model saved.")
