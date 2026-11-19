"""Real backend fault checks with synthetic inputs and temporary stores."""
import json,tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
out=[]
for model,url in [("synthetic:not-installed","http://127.0.0.1:11434"),("gemma4:26b","http://127.0.0.1:9")]:
    with tempfile.TemporaryDirectory() as root:
        s=Settings(cabinet_enabled=True,api_enabled=True,model_id=model,inference_url=url,
            cabinet_data_dir=root+"/cabinet",api_data_dir=root+"/api",inference_lock_dir=root+"/locks",
            readiness_timeout_seconds=.2,tokenizer_timeout=.2,connect_timeout_seconds=.2)
        app=create_app(s)
        with TestClient(app,base_url="https://synthetic.test") as client:
            key=app.state.api_keys.create("synthetic-fault")
            response=client.post("/v1/chat/completions",headers={"Authorization":"Bearer "+key["api_key"]},
                json={"model":"qwen3:8b","messages":[{"role":"user","content":'[{"item":1,"text":"Synthetic example"}]'}]})
            ready=client.get("/readyz")
            row={"configured_model":model,"url":url,"status":response.status_code,
                 "error_keys":sorted(response.json()["error"]),"error_code":response.json()["error"]["code"],
                 "ready_status":ready.status_code,"ready_reason":ready.json().get("reason")}
            assert response.status_code==503 and ready.status_code==503
            assert "choices" not in response.json()
            out.append(row)
Path("verification/fail-closed.json").write_text(json.dumps(out,indent=2))
print(json.dumps(out))
