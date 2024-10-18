
#%% Import statements

import io
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklego.preprocessing import RepeatingBasisFunction
import requests
import torch
import zipfile


#%% Download data set


# ElectricityLoadDiagrams20112014 from UCI Machine Learning Repository
url = "https://archive.ics.uci.edu/static/public/321/electricityloaddiagrams20112014.zip"
response = requests.get(url, stream=True)


#%% Extract the zip file

z = zipfile.ZipFile(io.BytesIO(response.content))
z.extractall()

#%% Read as pandas dataframe and set index

df = pd.read_csv("LD2011_2014.txt", delimiter=";", decimal=',')
df = df.rename(columns={"Unnamed: 0": "date"}).set_index("date")
df.index = pd.to_datetime(df.index)
df

#%% Skip 2011 since some clients are recorded from 2012

df = df[df.index > "2012-01-01 00:00:00"] 
df

#%% Preprocessing: Add 12 new features using Radial Basis Functions (RBFs) to encode time

df["day_of_year"] = df.index.day_of_year

rbf = RepeatingBasisFunction(n_periods=12,
                         	column="day_of_year",
                         	input_range=(1,365),
                         	remainder="drop")

rbf.fit(df)

df_rbf = pd.DataFrame(index=df.index, data=rbf.transform(df))
df_rbf

#%% Visualize the 12 features generated by RBFs

df_rbf.plot(subplots=True, figsize=(14, 8),
     	sharex=True, title="Radial Basis Functions",
     	legend=False)

#%% Choose a client (1-370) to train and test forecasting as dataset contains 

CLIENT = 1
suffix = (3 - len(str(CLIENT))) * "0" + str(CLIENT)  # determine num zeroes before id 
df_client = df[f"MT_{suffix}"]
df_client


# %% Train-test split

# train-test split for time series
train_size = int(len(df) * 0.67)
test_size = len(df) - train_size
df_train = df_client[:train_size]
df_test = df_client[train_size:]

 
def create_dataset(df, lookback):
    """Transform a time series into a prediction dataset
    
    Args:
        dataset: A numpy array of time series, first dimension is the time steps
        lookback: Size of window for prediction
    """
    X, y = [], []
    for i in range(len(df)-lookback):
        feature = df[i:i+lookback]
        target = df[i+1:i+lookback+1]
        X.append(feature)
        y.append(target)
    #return torch.tensor(X), torch.tensor(y)
    return X, y

LOOKBACK = 4 * 4 # consider last 4 hours in the window (15 min. intevals)
X_train, y_train = create_dataset(df_train, lookback=LOOKBACK)
X_test, y_test = create_dataset(df_test, lookback=LOOKBACK)


#%%

from torch.utils.data import Dataset, DataLoader


class UCIElectricityDataset(Dataset):
    def __init__(self, features, target, transform=None, target_transform=None):
        self.target = target
        #self.df = df
        self.features = features
        self.transform = transform
        self.target_transform = target_transform

    def __len__(self):
        return len(self.features)

    def __getitem__(self, idx):
        features = self.features[idx]
        target = self.target[idx]
        if self.transform:
            features = self.transform(features)
        if self.target_transform:
            target = self.target_transform(target)
        features = torch.Tensor(features)
        target = torch.Tensor(target)
        return features.unsqueeze(1), target.unsqueeze(1)


train_dataset = UCIElectricityDataset(X_train, y_train)
test_dataset = UCIElectricityDataset(X_test, y_test)

BATCH_SIZE = 64

train_dataloader = DataLoader(train_dataset, batch_size=BATCH_SIZE, shuffle=False)
test_dataloader = DataLoader(test_dataset, batch_size=BATCH_SIZE, shuffle=False)


# %%
# Display feature and target shapes.
train_features, train_labels = next(iter(train_dataloader))
print(f"Feature batch shape: {train_features.size()}")
print(f"Labels batch shape: {train_labels.size()}")

test_features, test_labels = next(iter(test_dataloader))
print(f"Feature batch shape: {test_features.size()}")
print(f"Labels batch shape: {test_labels.size()}")


# %% Implement the LSTM Model

import torch.nn as nn


class LSTMModel(nn.Module):
    def __init__(self, input_size, output_size, num_layers=1, hidden_size=16):
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=input_size, 
            hidden_size=hidden_size, 
            num_layers=num_layers, 
            batch_first=True) # TODO fix batch_first
        self.linear = nn.Linear(HIDDEN_SIZE, output_size)
    def forward(self, x):
        x, _ = self.lstm(x)
        x = self.linear(x)
        return x
    

#%% Configure the model and the experiment

import torch.optim as optim


DEVICE = "cuda"

NUM_LAYERS = 1
HIDDEN_SIZE = 50

model = LSTMModel(
    input_size=1, 
    output_size=1, 
    num_layers=NUM_LAYERS, 
    hidden_size=HIDDEN_SIZE)

optimizer = optim.Adam(model.parameters(), lr=0.001)
loss_fn = nn.MSELoss()

model = model.to(DEVICE)


#%% Train the model

from tqdm import tqdm


N_EPOCHS = 50
EVAL_PERIOD = 5

for epoch in range(N_EPOCHS):
    model.train()

    with tqdm(train_dataloader, unit="batch") as tepoch: # show progress bar for iterations
        for idx, batch in enumerate(tepoch):
            
            tepoch.set_description(f"Epoch {epoch}")

            X_batch = batch[0].to(DEVICE)
            y_batch = batch[1].to(DEVICE)

            y_pred = model(X_batch)
            loss = loss_fn(y_pred, y_batch)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            tepoch.set_postfix(loss=loss.item())
            
        # Validation
        if epoch % EVAL_PERIOD == EVAL_PERIOD - 1:
            print('validating...')
            model.eval()
            with torch.no_grad():
                
                sum_error = 0
                sum_len = 0
                last_preds = []

                for val_batch in test_dataloader:

                    X_val_batch = val_batch[0].to(DEVICE)
                    y_val_batch = val_batch[1].to(DEVICE)

                    y_pred_val = model(X_val_batch)

                    loss_val = loss_fn(y_pred_val, y_val_batch)
                    error_abs = np.abs(y_val_batch.cpu().numpy() - y_pred_val.detach().cpu().numpy())
                    sum_len += error_abs.size
                    sum_error += np.sum(error_abs)
                    last_preds.extend(y_pred_val[:,-1,:].cpu().numpy().squeeze())

            mae_val = sum_error / sum_len
            print(f"Epoch {epoch}: test MAE: {mae_val}")



# %% Plot real and predicted values

plt.plot(df_test.index, df_test.values, c='b', label="real")
plt.plot(df_test.index[LOOKBACK:], last_preds, c='r', label="predicted")
plt.grid()
plt.legend()
plt.show()

# %%