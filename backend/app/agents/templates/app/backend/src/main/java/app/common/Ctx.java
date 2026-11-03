package app.common;

/** 업무 로직에 넘기는 실행 정보: DB와 현재 사용자. */
public record Ctx(Db db, CurrentUser user) {
    public ApiError fail(int status, String code, String message) {
        return new ApiError(status, code, message);
    }
}
