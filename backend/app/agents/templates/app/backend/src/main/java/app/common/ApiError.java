package app.common;

/** API 오류. 응답은 openapi.yaml의 Error 형식 {code, message}. */
public class ApiError extends RuntimeException {
    private final int status;
    private final String code;

    public ApiError(int status, String code, String message) {
        super(message);
        this.status = status;
        this.code = code;
    }

    public int status() {
        return status;
    }

    public String code() {
        return code;
    }

    public static ApiError notImplemented(String what) {
        return new ApiError(501, "NOT_IMPLEMENTED", "구현 전: " + what);
    }

    public static ApiError notFound() {
        return new ApiError(404, "NOT_FOUND", "대상을 찾을 수 없습니다.");
    }
}
