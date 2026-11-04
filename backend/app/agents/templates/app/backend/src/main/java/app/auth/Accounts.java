package app.auth;

import app.common.ApiError;
import app.common.CurrentUser;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import javax.crypto.SecretKeyFactory;
import javax.crypto.spec.PBEKeySpec;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

/**
 * 시연용 로그인 계정과 세션. 역할마다 계정 하나 (resources/accounts.json, 스펙의 역할 목록에서 코드가 만든 것).
 * - 계정은 업무 테이블과 섞이지 않게 app_auth 스키마에 둔다.
 * - 처음 만드는 계정의 비밀번호는 DEMO_PASSWORD 또는 임의 값이다. 임의 값은 LOCAL_DIR/accounts.txt에만 적고 로그·응답에 남기지 않는다.
 * - 이미 있는 계정의 비밀번호는 다시 시작해도 바꾸지 않는다 (RESET_PASSWORDS=true면 모두 새로 만든다).
 * - 세션은 서버 메모리에 둔다. 서버를 다시 시작하면 다시 로그인한다.
 */
@Component
public class Accounts implements ApplicationRunner {
    public record Account(String loginId, String role, String userId, String name) {}

    private record Session(CurrentUser user, String loginId, Instant until) {}

    private static final Logger log = LoggerFactory.getLogger(Accounts.class);
    private static final int ITERATIONS = 120_000;
    private static final String LETTERS = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";

    private final JdbcTemplate db;
    private final List<Account> accounts;
    private final Map<String, Session> sessions = new ConcurrentHashMap<>();
    private final SecureRandom random = new SecureRandom();
    private final String demoPassword;
    private final boolean reset;
    private final Path localDir;
    private final Duration ttl;
    private final String dummy;

    public Accounts(JdbcTemplate db, ObjectMapper mapper,
                    @Value("${DEMO_PASSWORD:}") String demoPassword,
                    @Value("${RESET_PASSWORDS:false}") boolean reset,
                    @Value("${LOCAL_DIR:local}") String localDir,
                    @Value("${SESSION_HOURS:8}") long hours) throws IOException {
        this.db = db;
        try (InputStream in = new ClassPathResource("accounts.json").getInputStream()) {
            this.accounts = mapper.readValue(in, new TypeReference<List<Account>>() {});
        }
        this.demoPassword = demoPassword;
        this.reset = reset;
        this.localDir = Path.of(localDir);
        this.ttl = Duration.ofHours(Math.max(1, hours));
        this.dummy = hash("not-a-password");
    }

    @Override
    public void run(ApplicationArguments args) throws IOException {
        db.execute("CREATE SCHEMA IF NOT EXISTS app_auth");
        db.execute("CREATE TABLE IF NOT EXISTS app_auth.account ("
                + "login_id varchar(50) PRIMARY KEY, role varchar(100) NOT NULL, user_id varchar(50) NOT NULL, "
                + "password_hash varchar(200) NOT NULL, created_at timestamptz NOT NULL DEFAULT now())");
        if (reset) {
            db.update("DELETE FROM app_auth.account");
        }
        List<String> created = new ArrayList<>();
        for (Account a : accounts) {
            int found = db.update("UPDATE app_auth.account SET role = ?, user_id = ? WHERE login_id = ?", a.role(), a.userId(), a.loginId());
            if (found == 0) {
                String password = demoPassword.isEmpty() ? newPassword() : demoPassword;
                db.update("INSERT INTO app_auth.account (login_id, role, user_id, password_hash) VALUES (?, ?, ?, ?)",
                        a.loginId(), a.role(), a.userId(), hash(password));
                created.add(a.loginId() + "\t" + a.role() + "\t" + (demoPassword.isEmpty() ? password : "(DEMO_PASSWORD로 정한 값)"));
            }
        }
        List<String> ids = accounts.stream().map(Account::loginId).toList();
        for (String old : db.queryForList("SELECT login_id FROM app_auth.account", String.class)) {
            if (!ids.contains(old)) {
                db.update("DELETE FROM app_auth.account WHERE login_id = ?", old);   // 스펙에서 사라진 역할의 계정
            }
        }
        if (!created.isEmpty()) {
            Files.createDirectories(localDir);
            Path file = localDir.resolve("accounts.txt");
            String head = Files.exists(file) && !reset ? "" : "# 시연 계정 (아이디\t역할\t비밀번호). 새로 만든 계정만 적힌다. 이 파일은 공유하지 않는다.\n";
            StandardOpenOption mode = reset ? StandardOpenOption.TRUNCATE_EXISTING : StandardOpenOption.APPEND;
            Files.writeString(file, head + String.join("\n", created) + "\n", StandardCharsets.UTF_8, StandardOpenOption.CREATE, mode);
            log.info("시연 계정 {}개를 만들었습니다. 아이디와 비밀번호는 {}에 있습니다.", created.size(), file.toAbsolutePath());
        }
    }

    /** 로그인: 맞으면 세션 토큰과 사용자 정보 */
    public Map<String, String> login(String loginId, String password) {
        List<Map<String, Object>> rows = db.queryForList(
                "SELECT role, user_id, password_hash FROM app_auth.account WHERE login_id = ?", loginId == null ? "" : loginId);
        String stored = rows.isEmpty() ? dummy : (String) rows.get(0).get("password_hash");
        boolean matches = verify(password == null ? "" : password, stored);   // 없는 아이디도 같은 시간이 걸리게 한다
        if (rows.isEmpty() || !matches) {
            throw new ApiError(401, "UNAUTHORIZED", "아이디 또는 비밀번호를 확인하세요.");
        }
        sessions.entrySet().removeIf(e -> e.getValue().until().isBefore(Instant.now()));
        byte[] bytes = new byte[32];
        random.nextBytes(bytes);
        String token = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
        CurrentUser user = new CurrentUser(String.valueOf(rows.get(0).get("user_id")), (String) rows.get(0).get("role"));
        sessions.put(token, new Session(user, loginId, Instant.now().plus(ttl)));
        return Map.of("token", token, "loginId", loginId, "role", user.role(), "userId", user.id());
    }

    /** 토큰의 사용자 (없거나 만료면 빈 값) */
    public Optional<CurrentUser> user(String token) {
        return session(token).map(Session::user);
    }

    /** 토큰의 로그인 정보 */
    public Optional<Map<String, String>> me(String token) {
        return session(token).map(s -> Map.of("loginId", s.loginId(), "role", s.user().role(), "userId", s.user().id()));
    }

    public void logout(String token) {
        if (token != null) {
            sessions.remove(token);
        }
    }

    private Optional<Session> session(String token) {
        Session s = token == null ? null : sessions.get(token);
        if (s == null) {
            return Optional.empty();
        }
        if (s.until().isBefore(Instant.now())) {
            sessions.remove(token);
            return Optional.empty();
        }
        return Optional.of(s);
    }

    private String newPassword() {
        StringBuilder out = new StringBuilder();
        for (int i = 0; i < 12; i++) {
            out.append(LETTERS.charAt(random.nextInt(LETTERS.length())));
        }
        return out.toString();
    }

    private String hash(String password) {
        byte[] salt = new byte[16];
        random.nextBytes(salt);
        return "pbkdf2$" + ITERATIONS + "$" + HexFormat.of().formatHex(salt) + "$" + HexFormat.of().formatHex(derive(password, salt, ITERATIONS));
    }

    private boolean verify(String password, String stored) {
        String[] parts = stored.split("\\$");
        if (parts.length != 4 || !"pbkdf2".equals(parts[0])) {
            return false;
        }
        byte[] expected = HexFormat.of().parseHex(parts[3]);
        byte[] actual = derive(password, HexFormat.of().parseHex(parts[2]), Integer.parseInt(parts[1]));
        return MessageDigest.isEqual(expected, actual);
    }

    private static byte[] derive(String password, byte[] salt, int iterations) {
        try {
            PBEKeySpec spec = new PBEKeySpec(password.toCharArray(), salt, iterations, 256);
            return SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256").generateSecret(spec).getEncoded();
        } catch (GeneralSecurityException e) {
            throw new IllegalStateException("비밀번호 해시를 만들지 못했습니다.", e);
        }
    }
}
