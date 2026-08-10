package com.foo.service;
import com.foo.repo.UserRepository;
import com.foo.model.*;
import static com.foo.util.Helpers.log;
import org.springframework.stereotype.Service;
@Service
public class UserService extends AbstractService implements UserApi {
    private final UserRepository repo;
    public UserService(UserRepository repo){ this.repo=repo; }
    public User getUser(Long id){ log("get"); return repo.findById(id); }
}
