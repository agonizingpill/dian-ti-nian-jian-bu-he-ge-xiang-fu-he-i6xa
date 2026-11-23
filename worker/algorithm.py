from sklearn.tree import DecisionTreeClassifier, export_text
FEATURE_NAMES = ["critical_fail_count", "average_score", "failed_item_count", "documentation_gap"]
FEATURE_LABELS = {
    "critical_fail_count": "关键不合格项数",
    "average_score": "平均得分",
    "failed_item_count": "不合格项数",
    "documentation_gap": "资料缺项",
}


def _model():
    X = [[0,96,0,0],[0,84,1,0],[0,78,2,0],[0,72,2,1],[1,96,1,0],[1,68,3,1],[0,91,0,1],[0,66,3,1],[2,88,2,0]]
    y = ["ready","ready","review","hold","hold","hold","review","hold","hold"]
    model = DecisionTreeClassifier(max_depth=3, random_state=1066)
    model.fit(X, y)
    return model


def explain_path(model, vector):
    tree = model.tree_
    node = 0
    steps = []
    key_parts = []
    while tree.children_left[node] != -1:
        feature = FEATURE_NAMES[tree.feature[node]]
        threshold = float(tree.threshold[node])
        goes_left = vector[0][tree.feature[node]] <= threshold
        operator = "<=" if goes_left else ">"
        steps.append(f"{FEATURE_LABELS[feature]} {operator} {threshold:g}")
        key_parts.append(f"{feature}{operator}{threshold:g}")
        node = int(tree.children_left[node] if goes_left else tree.children_right[node])
    return {"leaf_id": node, "leaf_key": "&".join(key_parts) or "root", "branch": steps}


def classify(features):
    model = _model()
    vector = [[features[n] for n in FEATURE_NAMES]]
    prediction = model.predict(vector)[0]
    path = explain_path(model, vector)
    return {
        "prediction": prediction,
        "probability": float(max(model.predict_proba(vector)[0])),
        "path_rules": export_text(model, feature_names=FEATURE_NAMES),
        "leaf_id": path["leaf_id"],
        "leaf_key": path["leaf_key"],
        "branch": path["branch"],
        "branch_text": " → ".join([*path["branch"], str(prediction)]),
        "model": "DecisionTreeClassifier(random_state=1066,max_depth=3)",
    }
