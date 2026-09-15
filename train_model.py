import pandas as pd, joblib, json
from catboost import CatBoostRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import r2_score, mean_absolute_error

df=pd.read_csv("data.csv")
features=["State","City","Property_Type","Bedrooms","Bathrooms","Area_sqm","Age_Years","Parking_Spaces"]
X=df[features].copy(); y=df["Price_NGN"]
cat=["State","City","Property_Type"]
for c in cat: X[c]=X[c].astype(str)
Xtr,Xte,ytr,yte=train_test_split(X,y,test_size=.2,random_state=42)
model=CatBoostRegressor(iterations=500,depth=7,learning_rate=.05,loss_function="RMSE",verbose=False,random_seed=42)
model.fit(Xtr,ytr,cat_features=cat)
pred=model.predict(Xte)
metrics={"r2":r2_score(yte,pred),"mae":mean_absolute_error(yte,pred)}
joblib.dump(model,"model.joblib"); json.dump(metrics,open("metrics.json","w"),indent=2)
print(metrics)
