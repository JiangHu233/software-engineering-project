from studyroom.app import create_app

app = create_app()

if __name__ == "__main__":
    # use_reloader=False：避免 debug reloader 造成 APScheduler 双实例（§5.4/ADR-7 单实例约束）
    app.run(debug=True, use_reloader=False, port=int(__import__("os").environ.get("PORT", "5000")))
